#accelerate launch --config_file accelerate_config.yaml 
pytho3 train_4DD.py \
  --epochs 10 \
  --batch_size 2 \
  --train_file train.tsv \
  --dev_file dev.tsv \
  --max_previous_utterance 6 \
  --model_name bert-base-cased \
  --model_output output \
  --log_output log \
  --use_tqdm True
