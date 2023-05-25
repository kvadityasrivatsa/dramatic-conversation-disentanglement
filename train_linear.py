import os, sys
import os.path
import torch
import logging
import ast
import glob
import copy
import random
import argparse
import re
import pathlib
import math
import datetime
import time
import datasets
import transformers
import ortools
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as data
import numpy as np
import ortools.graph.pywrapgraph as pywrapgraph
from sklearn import metrics
from tqdm import tqdm, trange
from pprint import pformat
from torch.nn import CrossEntropyLoss
from torch.nn import CosineEmbeddingLoss
from torch import optim
from transformers import AutoModel, AutoConfig, AutoTokenizer 
from accelerate import Accelerator, DistributedDataParallelKwargs
from accelerate.logging import get_logger

from models import *
from eval import *

def set_seed(seed: int) -> None:
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic=True
    torch.backends.cudnn.benchmark=False
    os.environ["PYTHONHASHSEED"]=str(seed)
    main_log(f"Random seed set as {seed}")

def merge(sequences, ignore_idx=None):
    '''
    merge from batch * sent_len to batch * max_len 
    '''
    pad_token=0 if type(ignore_idx)==type(None) else ignore_idx
    #pad_token = PAD_token if type(ignore_idx)==type(None) else ignore_idx
    lengths=[len(seq) for seq in sequences]
    max_len=1 if max(lengths)==0 else max(lengths)
    padded_seqs=torch.ones(len(sequences), max_len).long() * pad_token 
    for i, seq in enumerate(sequences):
        end=lengths[i]
        padded_seqs[i, :end]=seq[:end]
    padded_seqs=padded_seqs.detach() #torch.tensor(padded_seqs)
    return padded_seqs, lengths


class CDDataset(torch.utils.data.Dataset):
    def __init__(self, lines, filename_to_filename_id, scene_id2line_ids, line_id2line_text, line_id2turn_n, line_id2speaker, speaker2line_ids, tokenizer, mode, max_length=512, num_negative_examples=10):
        self.tokenizer=tokenizer
        self.max_length=max_length
        
        self.num_negative_examples=num_negative_examples
        self.max_candidates=10
        self.max_distance=12
        
        self.filename_to_filename_id=filename_to_filename_id
        self.mode=mode
        self.lines=lines
        self.scene_id2line_ids=scene_id2line_ids
        self.line_id2line_text=line_id2line_text
        self.line_id2turn_n=line_id2turn_n
        self.line_id2speaker=line_id2speaker
        self.speaker2line_ids=speaker2line_ids
        
        self.start_token=self.tokenizer.cls_token  
        self.sep_token=self.tokenizer.sep_token
        self.line_token='[LINE]'
        self.self_token='[SELF]'
        self.mode_dict={'train': 0, 'dev': 1, 'test': 2}

        if self.mode == 'train':
            self.pool=self.produce_negative_examples()
        else:
            self.pool=self.produce_candidates()


            
    def __getitem__(self, idx):
        return self.pool[idx]
    
    def __len__(self):
        return len(self.pool)
    
    def tokenize_line(self, sequence):
        tokens=self.tokenizer.tokenize(sequence)[-self.max_length+1:]
        return torch.Tensor(self.tokenizer.convert_tokens_to_ids(tokens))
    

    def get_negative_line_ids(self, filename_id, scene_id, utterance_of_interest_id, true_parent_id, num_negative_examples):
        all_line_ids_in_scene=self.scene_id2line_ids[self.mode][filename_id][scene_id]
        candidate_line_ids=all_line_ids_in_scene[:all_line_ids_in_scene.index(utterance_of_interest_id)]
        all_negative_line_ids=[i 
                               for i in candidate_line_ids
                               if (i != true_parent_id) and (not i.startswith('A'))]
        random.shuffle(all_negative_line_ids)
        negative_ids=[]
        for negative_id in all_negative_line_ids:
            utterances_distance=abs(int(utterance_of_interest_id[1:])-int(negative_id[1:]))
            if utterances_distance < self.max_distance+1:
                if len(negative_ids) >= num_negative_examples:
                    return negative_ids[:num_negative_examples]
                negative_ids.append(negative_id)
        return negative_ids

    def get_candidate_line_ids(self, filename_id, scene_id, line_id):
        all_line_ids_in_scene=self.scene_id2line_ids[self.mode][filename_id][scene_id]
        candidate_line_ids=all_line_ids_in_scene[:all_line_ids_in_scene.index(line_id)]
        candidate_line_ids=[candidate_line_id for candidate_line_id in candidate_line_ids 
                            if (not candidate_line_id.startswith('A')) ]
        return candidate_line_ids
        
    def get_concat_context(self, filename_id, scene_id, line_id):
        all_line_ids_in_scene=self.scene_id2line_ids[self.mode][filename_id][scene_id]
        previous_line_ids=all_line_ids_in_scene[:all_line_ids_in_scene.index(line_id)]
        if not previous_line_ids:
            return ''
        
        try:
            all_line_ids_in_scene=self.scene_id2line_ids[self.mode][filename_id][scene_id]
            context_ids=all_line_ids_in_scene[:all_line_ids_in_scene.index(line_id)]
        except:
            print(filename_id, scene_id, line_id)
            sys.exit(1)
            
        context_tokens=[]
        for context_id in reversed(context_ids):
            tokens=tokenizer.tokenize(line_id2line_text[self.mode][filename_id][context_id])
            if len(context_tokens)<=self.max_length:
                context_tokens.extend(tokens)            
            
        return f"{self.start_token} {self.tokenizer.convert_tokens_to_string(context_tokens)}"

    def produce_candidates(self):
        pool=[]
        for key, line_data in self.lines[self.mode].items():    
            filename_id=line_data['filename_id']
            title=line_data['title']
            scene_id=line_data['scene_id']
            utterance_of_interest_id=key[1]
            utterance_of_interest_plain=f"{self.start_token} {self.line_id2line_text[self.mode][line_data['filename_id']][utterance_of_interest_id]}"
            utterance_of_interest=self.tokenize_line(utterance_of_interest_plain)

            true_parent_utterance_id=line_data['reply_to_id']
            scene_speaker_id=line_data['scene_speaker_id']
            
            # feat 1: How many utterances ago this character last spoke
            utterance_ids_by_same_speaker=self.speaker2line_ids[self.mode][line_data['filename_id']][scene_speaker_id]
            last_spoke=0
            last_utterance_id_by_same_speaker=utterance_ids_by_same_speaker[utterance_ids_by_same_speaker.index(utterance_of_interest_id)-1]
            last_spoke=abs(int(utterance_of_interest_id[1:])-int(last_utterance_id_by_same_speaker[1:]))

            # feat 2: Is the next utterance spoken by the same character?
            next_same=0
            next_utterance_id_int=int(utterance_of_interest_id[1:])+1
            if f"D{next_utterance_id_int}" in utterance_ids_by_same_speaker:
                next_same=1

            candidate_line_ids=self.get_candidate_line_ids(filename_id, scene_id, utterance_of_interest_id)
            candidate_line_ids=list(reversed(candidate_line_ids))
            
            if self.max_candidates:
                candidate_line_ids=candidate_line_ids[:self.max_candidates]

            for candidate_line_id in candidate_line_ids:
                candidate_line_plain=f"{self.start_token} {self.line_id2line_text[self.mode][line_data['filename_id']][candidate_line_id]}"
                candidate_line=self.tokenize_line(candidate_line_plain) 

                # feat 3: number of words in common
                c_words_in_common=len(set(candidate_line) & set(utterance_of_interest))

                # feat 4
                utterances_distance=int(utterance_of_interest_id[1:])-int(candidate_line_id[1:]) 

                # feat 5: in between, are there messages from either speaker
                has_message_inbetween=0
                speaker_ids=[]
                for i in (utterance_of_interest_id, candidate_line_id):
                    if i in self.line_id2speaker[self.mode][line_data['filename_id']]:
                        speaker_ids.append(self.line_id2speaker[self.mode][line_data['filename_id']][i])
                for utterance_id in range(int(utterance_of_interest_id[1:]), int(candidate_line_id[1:])+1):
                    if has_message_inbetween==1:
                        continue
                    if f"D{utterance_id}" in self.line_id2speaker[self.mode][line_data['filename_id']]:
                        if self.line_id2speaker[self.mode][line_data['filename_id']][f"D{utterance_id}"] in speaker_ids:
                            has_message_inbetween=1
                                            
                turn_a=self.line_id2turn_n[self.mode][line_data['filename_id']][utterance_of_interest_id]
                turn_b=self.line_id2turn_n[self.mode][line_data['filename_id']][candidate_line_id]
                speaker_a=self.line_id2speaker[self.mode][line_data['filename_id']][utterance_of_interest_id]
                speaker_b=self.line_id2speaker[self.mode][line_data['filename_id']][candidate_line_id]

                if candidate_line_id == true_parent_utterance_id:
                    y=1
                else:
                    y=0

                item={'filename_id': filename_id,
                      'utterance_of_interest_id': utterance_of_interest_id,
                      'candidate_line_id': candidate_line_id, 
                      'scene_speaker_order': scene_speaker_id,
                      'last_spoke': last_spoke,
                      'next_same': next_same,
                      'c_words_in_common': c_words_in_common,                      
                      'utterances_distance': utterances_distance,
                      'has_message_inbetween': has_message_inbetween,
                      'first_spoke': 1 if scene_speaker_id=='0' else 0,
                      'same_turn': 1 if turn_a == turn_b else 0,
                      'same_speaker': 1 if speaker_a == speaker_b else 0,
                      'mode': self.mode_dict[self.mode],
                      'label': y
                }

                pool.append(item)

        return pool    
    
    def produce_negative_examples(self):
        pool=[]
        for key, line_data in self.lines[self.mode].items():    
            filename_id=line_data['filename_id'] 
            title=line_data['title']
            scene_id=line_data['scene_id']
            utterance_of_interest_id=key[1]
            utterance_of_interest_plain=f"{self.start_token} {self.line_id2line_text[self.mode][line_data['filename_id']][utterance_of_interest_id]}"
            utterance_of_interest=self.tokenize_line(utterance_of_interest_plain)    

            true_parent_utterance_id=line_data['reply_to_id']
            scene_speaker_id=line_data['scene_speaker_id']

            # feat 1: How many utterances ago this character last spoke
            utterance_ids_by_same_speaker=self.speaker2line_ids[self.mode][line_data['filename_id']][scene_speaker_id]
            last_spoke=0
            last_utterance_id_by_same_speaker=utterance_ids_by_same_speaker[utterance_ids_by_same_speaker.index(utterance_of_interest_id)-1]
            last_spoke=abs(int(utterance_of_interest_id[1:])-int(last_utterance_id_by_same_speaker[1:]))

            # feat 2: Is the next utterance spoken by the same character?
            next_same=0
            next_utterance_id_int=int(utterance_of_interest_id[1:])+1
            if f"D{next_utterance_id_int}" in utterance_ids_by_same_speaker:
                next_same=1

            item={'filename_id': filename_id,
                    'utterance_of_interest_id': utterance_of_interest_id,
                    'candidate_line_id': true_parent_utterance_id,
                    'scene_speaker_order': scene_speaker_id,
                    'last_spoke': last_spoke,
                    'next_same': next_same,
                    'c_words_in_common': len(utterance_of_interest),
                    'utterances_distance': 0,  
                    'has_message_inbetween': 0,
                    'first_spoke': 1 if scene_speaker_id=='0' else 0,
                    'same_turn': 1,
                    'same_speaker': 1,
                    'mode': self.mode_dict[self.mode],
                    'label': 1 if true_parent_utterance_id.startswith('T') else 0
                    }

            pool.append(item)

            if true_parent_utterance_id.startswith('D'):
                true_parent_utterance_plain=f"{self.start_token} {self.line_id2line_text[self.mode][line_data['filename_id']][true_parent_utterance_id]}"
                true_parent_utterance=self.tokenize_line(true_parent_utterance_plain)

                # feat 3: number of words in common
                c_words_in_common=len(set(true_parent_utterance) & set(utterance_of_interest))
                
                # feat 4: utterance distance
                try:
                    utterances_distance=int(utterance_of_interest_id[1:])-int(true_parent_utterance_id[1:]) 
                except:
                    main_log((utterance_of_interest_id, true_parent_utterance_id))
                    sys.exit(1)

                # feat 5: in between, are there messages from either speaker
                has_message_inbetween=0
                speaker_ids=[]
                for i in (utterance_of_interest_id, true_parent_utterance_id):
                    if i in self.line_id2speaker[self.mode][line_data['filename_id']]:
                        speaker_ids.append(self.line_id2speaker[self.mode][line_data['filename_id']][i])
                for utterance_id in range(int(utterance_of_interest_id[1:]), int(true_parent_utterance_id[1:])+1):
                    if has_message_inbetween==1:
                        continue
                    if f"D{utterance_id}" in self.line_id2speaker[self.mode][line_data['filename_id']]:
                        if self.line_id2speaker[self.mode][line_data['filename_id']][f"D{utterance_id}"] in speaker_ids:
                            has_message_inbetween=1

                turn_a=self.line_id2turn_n[self.mode][line_data['filename_id']][utterance_of_interest_id]
                turn_b=self.line_id2turn_n[self.mode][line_data['filename_id']][true_parent_utterance_id]
                speaker_a=self.line_id2speaker[self.mode][line_data['filename_id']][utterance_of_interest_id]
                speaker_b=self.line_id2speaker[self.mode][line_data['filename_id']][true_parent_utterance_id]

                item={'filename_id': filename_id,
                      'utterance_of_interest_id': utterance_of_interest_id,
                      'candidate_line_id': true_parent_utterance_id,
                      'scene_speaker_order': scene_speaker_id,
                      'last_spoke': last_spoke,
                      'next_same': next_same,
                      'c_words_in_common': c_words_in_common,
                      'utterances_distance': utterances_distance,  
                      'has_message_inbetween': has_message_inbetween,
                      'first_spoke': 1 if scene_speaker_id=='0' else 0,
                      'same_turn': 1 if turn_a == turn_b else 0,
                      'same_speaker': 1 if speaker_a == speaker_b else 0,
                      'mode': self.mode_dict[self.mode],
                      'label': 1}

                pool.append(item)

            if true_parent_utterance_id.startswith('D'): # need one less neg ex
                num_negative_examples=self.num_negative_examples-1
            else:
                num_negative_examples=self.num_negative_examples

            negative_ids=self.get_negative_line_ids(filename_id, scene_id, utterance_of_interest_id, true_parent_utterance_id, num_negative_examples)
            
            if not negative_ids:
                # print(title, scene_id, utterance_of_interest_id, true_parent_utterance_id)
                continue
            
            for negative_id in negative_ids:                
                negative_parent_utterance_plain=f"{self.start_token} {self.line_id2line_text[self.mode][line_data['filename_id']][negative_id]}"
                negative_parent_utterance=self.tokenize_line(negative_parent_utterance_plain)            

                # feat 3: number of words in common
                c_words_in_common=len(set(negative_parent_utterance) & set(utterance_of_interest))

                # feat 4
                utterances_distance=int(utterance_of_interest_id[1:])-int(negative_id[1:])

                # feat 5: in between, are there messages from either speaker
                has_message_inbetween=0
                speaker_ids=[]
                for i in (utterance_of_interest_id, negative_id):
                    if i in self.line_id2speaker[self.mode][line_data['filename_id']]:
                        speaker_ids.append(self.line_id2speaker[self.mode][line_data['filename_id']][i])
                for utterance_id in range(int(utterance_of_interest_id[1:]), int(negative_id[1:])+1):
                    if has_message_inbetween==1:
                        continue
                    if f"D{utterance_id}" in self.line_id2speaker[self.mode][line_data['filename_id']]:
                        if self.line_id2speaker[self.mode][line_data['filename_id']][f"D{utterance_id}"] in speaker_ids:
                            has_message_inbetween=1

                turn_a=self.line_id2turn_n[self.mode][line_data['filename_id']][utterance_of_interest_id]
                turn_b=self.line_id2turn_n[self.mode][line_data['filename_id']][negative_id]
                speaker_a=self.line_id2speaker[self.mode][line_data['filename_id']][utterance_of_interest_id]
                speaker_b=self.line_id2speaker[self.mode][line_data['filename_id']][negative_id]          
                y=0

                item={'filename_id': filename_id,
                    'utterance_of_interest_id': utterance_of_interest_id,
                    'candidate_line_id': negative_id,                      
                    'scene_speaker_order': scene_speaker_id,
                    'last_spoke': last_spoke,
                    'next_same': next_same,
                    'c_words_in_common': c_words_in_common,
                    'utterances_distance': utterances_distance,
                    'has_message_inbetween': has_message_inbetween,
                    'first_spoke': 1 if scene_speaker_id=='0' else 0, 
                    'same_turn': 1 if turn_a == turn_b else 0,
                    'same_speaker': 1 if speaker_a == speaker_b else 0,
                    'mode': self.mode_dict[self.mode],
                    'label': y}
                pool.append(item)
        
        return pool

def to_cuda(x):
    if torch.cuda.is_available(): x=x.cuda()
    return x

def to_cpu(x):
    if type(x)==torch.Tensor: x=x.cpu()
    return x

def get_distance_bucket(d):
    if d < 4:
        return d
    if d>=4 and d<7:
        return 4
    if d>=7:
        return 5


def collate_fn_cd(data):
    entry={}
    for key in data[0].keys():
        entry[key] = [d[key] for d in data]

    # merge sequences  
    filename_id=entry['filename_id'] # a
    candidate_line_id=[int(id[1:]) for id in entry['candidate_line_id']] #b
    utterance_of_interest_id=[int(id[1:]) for id in entry['utterance_of_interest_id']] # c
    scene_speaker_order=[int(i) for i in entry['scene_speaker_order']] # d
    last_spoke=[i for i in entry['last_spoke']] # e
    next_same=[i for i in entry['next_same']] # f
    c_words_in_common=[i for i in entry['c_words_in_common']] # g
    utterances_distance=[abs(get_distance_bucket(d)) for d in entry['utterances_distance']] # h
    has_message_inbetween=[i for i in entry['has_message_inbetween']] # i
    # first_spoke=[i for i in entry['first_spoke']] #j
    same_turn=[st for st in entry['same_turn']] # k
    same_speaker=[sp for sp in entry['same_speaker']] # l
    mode=[i for i in entry['mode']]  #m
    label=entry['label'] # n
    
    x, y=[], []
    for a, b, c, d, e, f, g, h, i , j, k, l, m \
        in zip(filename_id, candidate_line_id, utterance_of_interest_id,
               scene_speaker_order, last_spoke, next_same, c_words_in_common, 
               utterances_distance, has_message_inbetween, same_turn, same_speaker, mode, label):
        x.append([a, b, c, d, e, f, g, h, i, j, k, l])
        y.append(m)
        
#     print(x)
    
    
    collated_data={'x': torch.FloatTensor(x), 'y': torch.LongTensor(y)}
    
    return collated_data

def main_log(msg):
    global logger
    return logger.info(msg, main_process_only=True)


def gen_file_lines(file_path):
    with open(file_path, 'r') as f:
        for line in f:
            line=line.replace('\n', '')
            if line.startswith('category\t'):
                continue
            cols=line.split('\t')

            if len(cols) == 14:
                # main_log(len(cols))
                category, filename, title, _, turn_line_no, scene_id, _,  _, line_no, speaker_label, _, _, anno, line_text=cols
            if len(cols) == 13:
                # main_log(len(cols))
                # if not str.isalpha(cols[-4]): # -4 = show_speaker_id or speaker label
                #     main_log('a ' + cols[-4])
                #     category, filename, title, _, turn_line_no, scene_id, _,  line_no,    speaker_label, _, _, anno, line_text=cols
                # else:
                #     main_log('b ' + cols[-4])
                category, filename, title, _, turn_line_no, scene_id, _,  _, line_no, speaker_label, _,    anno, line_text=cols


            if len(cols) == 12:
                # main_log(len(cols))
                category, filename, title, _, turn_line_no, scene_id, _,  _, line_no, speaker_label,       anno, line_text=cols
            if len(cols) == 11:
                # main_log(len(cols))
                category, filename, title, _, turn_line_no, scene_id, _,  line_no,    speaker_label,       anno, line_text=cols
            if len(cols) == 10:
                # main_log(len(cols))
                category, filename, title, _, turn_line_no, scene_id,     line_no,    speaker_label,       anno, line_text=cols
                
            yield category, filename, title, turn_line_no, scene_id, line_no, speaker_label, anno, line_text

if __name__=='__main__':

    arg_parser=argparse.ArgumentParser()
    arg_parser.add_argument('--encoder_name', help='specify encoder_name')
    arg_parser.add_argument('--epochs', help='specify epochs', default=8)    
    arg_parser.add_argument('--num_negative_examples', help='specify num_negative_examples', default=10)
    arg_parser.add_argument('--train_file', help='specify train_file')
    arg_parser.add_argument('--dev_file', help='specify dev_file')
    # arg_parser.add_argument('--test_file', help='specify test_file')
    arg_parser.add_argument('--model_name', help='specify model_name')

    arg_parser.add_argument("--use_tqdm",
                        default=False,
                        type=bool,
                        help="Use tqdm?")

    arg_parser.add_argument("--batch_size",
                        default=4,
                        type=int,
                        help="specific batch_size.")

    args=vars(arg_parser.parse_args())

    ROOT=pathlib.Path('/global/scratch/users/kentkchang/dramatic-bert')
    MODEL_PATH=ROOT / 'model'
    DATA_PATH=ROOT / 'model' / 'data'
    OUTPUT_PATH=MODEL_PATH / 'output_linear2'
    OUTPUT_PATH.mkdir(exist_ok=True)
    LOG_PATH=MODEL_PATH / 'logs_linear2'
    LOG_PATH.mkdir(exist_ok=True)

    args['epochs']=int(args['epochs'])
    # args['model_name']='google/electra-base-discriminator' #'roberta-base'#='bert-base-cased'
    args["n_gpu"]=torch.cuda.device_count()
    args["learning_rate"]=1e-2
    args['grad_clip']=1
    args["fix_encoder"]=0
    args["output_dir"]=OUTPUT_PATH
    args["logs_dir"]=str(LOG_PATH)

    use_tqdm=args['use_tqdm']
    BATCH_SIZE=args['batch_size']

    ######

    ddp_kwargs=DistributedDataParallelKwargs(find_unused_parameters=False)
    accelerator=Accelerator(kwargs_handlers=[ddp_kwargs])

    ######
    timestamp=datetime.datetime.now().strftime("%m%d%Y-%H%M%S")    
    logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename=os.path.join(args["logs_dir"], f"train_{timestamp}.log"),
                    filemode='w')
    console=logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter=logging.Formatter('%(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

    datasets.utils.logging.set_verbosity_error()
    transformers.utils.logging.set_verbosity_error()

    logger=get_logger(__name__)

    set_seed(2022)#int(datetime.datetime.now().strftime("%Y%m%d")))
    accelerator.wait_for_everyone()    
    ##
    main_log('v5')

    main_log(args['train_file'])
    main_log('Analyzing files ...')

    file_paths={
        'train': (DATA_PATH).joinpath(args['train_file']),
        'dev': (DATA_PATH).joinpath(args['dev_file']),
        # 'test': (DATA_PATH).joinpath(args['test_file']),
    }

    lines={'train': {}, 'dev': {}, 'test': {}}
    scene_id2line_ids={'train': {}, 'dev': {}, 'test': {}}
    line_id2line_text={'train': {}, 'dev': {}, 'test': {}}
    line_id2turn_n={'train': {}, 'dev': {}, 'test': {}}
    line_id2speaker={'train': {}, 'dev': {}, 'test': {}}
    speaker2line_ids={'train': {}, 'dev': {}, 'test': {}}
    speakers={'train': {}, 'dev': {}, 'test': {}}

    filename_to_filename_id={}
    last_scene_id=''
    last_turn_line_no=''
    scene_line_ids=[]

    for mode, file_path in file_paths.items():
        for line in gen_file_lines(file_path):
            category, filename, title, turn_line_no, scene_id, line_no, speaker_label, anno, line_text=line
            # main_log((category, filename, title, turn_line_no, scene_id, line_no, speaker_label, anno, line_text))
            # sys.exit(1)

            if filename not in speakers[mode]:
                speakers[mode][filename]={}
            if filename not in filename_to_filename_id:
                filename_to_filename_id[filename]=len(filename_to_filename_id)        

            filename_id=filename_to_filename_id[filename]

            if filename_id not in speakers[mode]:
                scene_speaker_id=speakers[mode][filename_id]={}

            if (line_no.startswith('D')):  
                speakers[mode][filename_id][speaker_label]=len(speakers[mode][filename_id])
                scene_speaker_id=speakers[mode][filename_id][speaker_label]

                if speaker_label:
                    speaker_label=speaker_label.lower() + ' [SEP] ' # take care of action lines
                lines[mode][(filename_id, line_no)]={
                    'corpus': category,
                    'filename_id': filename_id,
                    'title': title,
                    'scene_id': scene_id,
                    'scene_speaker_id': scene_speaker_id,
                    'turn_line_no': turn_line_no,
                    'reply_to_id': anno
                }
                last_turn_line_no=turn_line_no

            if filename_id not in line_id2line_text[mode]:
                line_id2line_text[mode][filename_id]={}             
            line_id2line_text[mode][filename_id][line_no]=f"{speaker_label}{line_text} [LINE]"

            if filename_id not in scene_id2line_ids[mode]:
                scene_id2line_ids[mode][filename_id]={}                         
            if scene_id not in scene_id2line_ids[mode][filename_id]:
                scene_id2line_ids[mode][filename_id][scene_id]=[]

            if line_no.startswith('A') or line_no.startswith('D'):
                scene_id2line_ids[mode][filename_id][scene_id].append(line_no)

            if filename_id not in line_id2turn_n[mode]:
                line_id2turn_n[mode][filename_id]={}   
            if line_no.startswith('D'):
                line_id2turn_n[mode][filename_id][line_no]=turn_line_no

            if filename_id not in line_id2speaker[mode]:
                line_id2speaker[mode][filename_id]={}   
            if filename_id not in speaker2line_ids[mode]:
                speaker2line_ids[mode][filename_id]={} 

            if line_no.startswith('D'):
                line_id2speaker[mode][filename_id][line_no]=scene_speaker_id
                if scene_speaker_id not in speaker2line_ids[mode][filename_id]:
                    speaker2line_ids[mode][filename_id][scene_speaker_id]=[]
                speaker2line_ids[mode][filename_id][scene_speaker_id].append(line_no)

    reversed_filename_to_filename_id={v: k for k, v in filename_to_filename_id.items()}
    
    model_name=args['model_name'] #"bert-base-cased" #allenai/longformer-base-4096 
    main_log(f'Initiating the model: {model_name}')

    num_negative_examples=int(args['num_negative_examples'])

    main_log(f'num_negative_examples: {num_negative_examples}')

    model=AutoModel.from_pretrained(model_name)
    config=AutoConfig.from_pretrained(model_name)
    # tokenizer=AutoTokenizer.from_pretrained(model_name, do_lower_case=False, do_basic_tokenize=False, local_files_only=True) 
    tokenizer=AutoTokenizer.from_pretrained(model_name, do_lower_case=False, do_basic_tokenize=False) 

    LINE_TOKEN='[LINE]'
    SELF_TOKEN='[SELF]'

    tokenizer.add_tokens([LINE_TOKEN, SELF_TOKEN], special_tokens=True)
    model.resize_token_embeddings(len(tokenizer))

    HIDDEN_DIM=config.hidden_size
    SEQUENCE_MAX_LEN=tokenizer.model_max_length

    dataset=CDDataset(lines, filename_to_filename_id, scene_id2line_ids, line_id2line_text, line_id2turn_n, line_id2speaker, speaker2line_ids, tokenizer, 'train', SEQUENCE_MAX_LEN, num_negative_examples)
    data_loader=torch.utils.data.DataLoader(dataset=dataset,
                                        batch_size=BATCH_SIZE,
                                        collate_fn=collate_fn_cd,
                                        shuffle=True, 
                                        drop_last=True)
                                        
    # for idx, item in enumerate(data_loader):
    #     # if idx<60:
    #     #     continue
    #     if idx>200:
    #         break
    #     del item['context']
    #     del item['filename_id']
    #     del item['parent_utterance']
    #     del item['utterance_of_interest']
    #     del item['utterances_distance']
    #     del item['same_turn']

    #     if int(item['candidate_line_id']) == 99999:
    #         main_log(pformat(item)) 
    #         # main_log('\n')
    # sys.exit(1)

    dev_dataset=CDDataset(lines, filename_to_filename_id, scene_id2line_ids, line_id2line_text, line_id2turn_n, line_id2speaker, speaker2line_ids, tokenizer, 'dev', SEQUENCE_MAX_LEN)
    dev_data_loader=torch.utils.data.DataLoader(dataset=dev_dataset,
                                            batch_size=BATCH_SIZE,
                                            collate_fn=collate_fn_cd,
                                            drop_last=True)

    # distances=[]
    # for i in data_loader:
    #     distances.append(i['utterances_distance'].tolist())
    # distance_embedding_dim=len(set([i for sublist in distances for i in sublist]))+1

    # args["distance_embedding_dim"]=distance_embedding_dim
    # args["distance_embedding_size"]=10


    #######
    main_log('Analyzing dev files ...')
    gold_threads={}
    gold_reply_to_dict={}

    mode='dev'
    
    dev_lines=[]
    for line in gen_file_lines(file_paths[mode]):
        # line=line.replace('\n', '')
        category, filename, title, turn_line_no, scene_id, line_no, speaker_label, anno, line_text=line#.split('\t')
        #category, filename, title, file_line_no, turn_line_no, scene_id, line_type, line_no, new_line_no, speaker_label, scene_speaker_id, anno, line_text=line.split('\t')

        if not line_no.startswith('D'):
            continue

        if filename not in gold_reply_to_dict:
            gold_reply_to_dict[filename]={}
            
        gold_reply_to_dict[filename][line_no]=anno
        
        if anno.startswith('T'):
            if filename not in gold_threads:
                gold_threads[filename]={}
            if anno not in gold_threads[filename]:
                gold_threads[filename][anno]=[]
            dev_lines.append([filename, line_no, anno])
            continue
            
        dev_lines.append([filename, line_no, anno])

    gold, _=eval_lines_dict_to_clusters(eval_lines_to_lines_dict(dev_lines))

    ####### 

    # model=LineDualEncoder(args)
    main_log(f"Enocder: {args['encoder_name']}")
    model=globals()[args['encoder_name']](args)
    # model=globals()[args['encoder_name']]()
    optimizer=optim.Adam(model.parameters(), lr=args["learning_rate"])
    criterion=nn.BCEWithLogitsLoss()

    model, optimizer, data_loader, dev_data_loader = accelerator.prepare(
        model, optimizer, data_loader, dev_data_loader
    )

    seen={}

    best_eval_metric=0.

    all_predictions_made, correct_predictions_made=0, 0

    num_steps=args["epochs"]*len(data_loader)
    # progress_bar=tqdm(num_steps)

    if use_tqdm:
        progress_bar=tqdm(num_steps)

    main_log('Training starts ...')
    timestamp=datetime.datetime.now().strftime("%m%d%Y-%H%M%S")    
    # best_model_path=f"pytorch_model-{timestamp}.bin"
    for epoch in range(args["epochs"]):
        main_log("Epoch:{}".format(epoch+1)) 
        train_loss = 0
        # pbar=tqdm(data_loader)
        for i, d in enumerate(data_loader):#enumerate(pbar):
    #         if i > 10:
    #             break
            model.train()
            # main_log(d)
            d={key: to_cuda(val) for key, val in d.items()}

            x, y=d['x'], d['y']

            # sys.exit(1)

            outputs=model(x)
            outputs['label']=y
            
            # if torch.cuda.device_count() > 1:
                # loss=model.module.criterion(outputs['logits'], outputs['label'].float())
            # else:

            loss=criterion(outputs['logits'], outputs['label'].float())

            # for name, param in model.named_parameters():
            #     if param.grad is None:
            #         print(name)
            # loss.backward() 

            train_loss += loss.item()
            # train_step += 1

            accelerator.backward(loss)            
            optimizer.step()
            optimizer.zero_grad()

            if use_tqdm:
                progress_bar.update(1)
                progress_bar.set_description("training Loss: {:.4f}".format(train_loss/(i+1))) 

            if (i == len(data_loader)-1):
                model.eval()

                # main_log(pformat(seen))
                # sys.exit(1)

                pred_correct, pred_total=0., 0.
                preds_dict={}
                # ppbar=tqdm(dev_data_loader)
                ys, y_preds=[], []

                for idx, d in enumerate(dev_data_loader):#enumerate(ppbar):
                    d={key: to_cuda(val) for key, val in d.items()} 
                    x, y=d['x'], d['y']
                    with torch.no_grad():        
                        outputs=model(x)
                        outputs['label']=y
                        outputs=accelerator.gather(outputs)

                        for filename_id, utterance_of_interest_id, candidate_line_id, logit, label in \
                            zip(outputs['filename_id'], outputs['utterance_of_interest_id'], outputs['candidate_line_id'], outputs['logits'], outputs['label']):

                            pred=1 if to_cpu(torch.sigmoid(logit))>0.5 else 0
                            label=int(to_cpu(label))

                            filename_id, utterance_of_interest_id, candidate_line_id, logit, score, pred, label=\
                                (int(to_cpu(filename_id)), 
                                int(to_cpu(utterance_of_interest_id)), 
                                int(to_cpu(candidate_line_id)), 
                                to_cpu(logit), 
                                torch.sigmoid(logit),
                                pred,
                                label)
                            
                            ys.append(label)
                            y_preds.append(pred)

                            if filename_id not in preds_dict:
                                preds_dict[filename_id]={}

                            if utterance_of_interest_id not in preds_dict[filename_id]:
                                preds_dict[filename_id][utterance_of_interest_id]={'scores': []}

                            preds_dict[filename_id][utterance_of_interest_id]['scores'].append(
                                (candidate_line_id, score)
                            )
                
                # main_log(pformat(preds_dict))

                last_filename=''
                preds_lines=[]
                for filename_id, utterances_of_interest_dict in preds_dict.items():
                    filename=reversed_filename_to_filename_id[filename_id]
                    if last_filename != filename:
                        last_filename=filename
                        threads_predicted=0
                    for utterance_of_interest in utterances_of_interest_dict:
                        scores=utterances_of_interest_dict[utterance_of_interest]['scores']
                        scores=sorted(scores, key=lambda tup: tup[1], reverse=True)
                        best_parent_id=scores[0][0]
                        best_score=scores[0][1]

                        if (best_parent_id == utterance_of_interest):# or (best_score < 0.9):
                            final_pred=f"T{threads_predicted}"
                            threads_predicted += 1
                            # main_log(f'Threads predicted! Gold: {gold_reply_to_dict[filename][f"D{utterance_of_interest}"]}')
                        else:                            
                            final_pred='D'+str(best_parent_id)
                        preds_lines.append([filename, f"D{utterance_of_interest}", final_pred])  
                        if f"D{utterance_of_interest}" in gold_reply_to_dict[filename]:
                            if final_pred==gold_reply_to_dict[filename][f"D{utterance_of_interest}"]:
                                correct_predictions_made += 1
                            all_predictions_made +=1                           

                
                if (correct_predictions_made/all_predictions_made)>0:
                    p, r, f=get_average_scores(ys, y_preds)
                    auto, _=eval_lines_dict_to_clusters(eval_lines_to_lines_dict(preds_lines))
                    contingency, row_sums, col_sums=None, None, None
                    contingency, row_sums, col_sums=clusters_to_contingency(gold, auto)

                    pairwise_accuracy=correct_predictions_made/all_predictions_made
                    ari=adjusted_rand_index(contingency, row_sums, col_sums)
                    vi=variation_of_information(contingency, row_sums, col_sums)
                    shen=shen_f1(contingency, row_sums, col_sums, gold, auto)
                    oto=one_to_one(contingency, row_sums, col_sums)
                    # exact_match(gold, auto, skip_single=False)
                    em_f=exact_match(gold, auto, skip_single=False)
                    
                    cluster_metrics=(ari+vi+shen+em_f)/4
                    eval_metric=0.2*pairwise_accuracy+0.8*cluster_metrics
                    exact_match(gold, auto)

                    if eval_metric>best_eval_metric:
                        best_model_path=f"linear_model-{timestamp}.bin"
                        # best_model_path=f"pytorch_model-{timestamp}-epoch{epoch}.bin"
                        main_log(f"New best -- Model saved: {best_model_path}")
                        # main_log(pformat(preds_dict))
                        best_eval_metric=eval_metric
                        main_log(f"Pairwise_accuracy: {pairwise_accuracy*100:.2f}; p, r, f: {p*100:.2f}, {r*100:.2f}, {f*100:.2f}; ari: {ari:5.2f}; 1-vi: {vi:.2f}; shen: {shen:.2f}; oto: {oto:.2f}; em-f: {em_f:.2f}")
                        # torch.save(model.state_dict(), best_model_path)
                        accelerator.wait_for_everyone()
                        unwrapped_model=accelerator.unwrap_model(model)
                        accelerator.save(unwrapped_model.state_dict(), str(pathlib.Path(args["output_dir"].joinpath(best_model_path))))
                # print('---------------------------------------------')