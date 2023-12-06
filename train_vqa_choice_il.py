# Standard library imports
import argparse
import json
import logging
import math
import os
import random
import re
from functools import partial
from pathlib import Path

# Third-party imports
from PIL import Image
import datasets
from datasets import Dataset, load_metric
import torch
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from tqdm import tqdm
from tqdm.auto import tqdm
import transformers
from transformers import (
    AdamW,
    AutoConfig,
    AutoImageProcessor,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    BertModel,
    BertConfig,
    DataCollatorWithPadding,
    PretrainedConfig,
    PreTrainedModel,
    SchedulerType,
    default_data_collator,
    get_scheduler,
    T5Tokenizer,
    T5ForConditionalGeneration,
)
from transformers.modeling_outputs import SequenceClassifierOutput
from transformers.utils.versions import require_version
import torch.nn as nn
import torch.nn.functional as F
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed
from huggingface_hub import Repository
import wandb

from transformers import XLMRobertaTokenizer
from vqa import _get_base_config, BEiT3ForVisualQuestionAnswering

# Local application/library specific imports


JSON_PATH = "../data/items_shuffle.json"
TRAJ_PATH = "data/il_trajs_finalized_images.jsonl"
GOAL_PATH = "data/human_goals.json"
IMAGE_PATH = "../all_images"
IMAGE_SIZE = 224
CKPT_PATH = "ckpts"

task_to_keys = {
    "cola": ("sentence", None),
    "mnli": ("premise", "hypothesis"),
    "mrpc": ("sentence1", "sentence2"),
    "qnli": ("question", "sentence"),
    "qqp": ("question1", "question2"),
    "rte": ("sentence1", "sentence2"),
    "sst2": ("sentence", None),
    "stsb": ("sentence1", "sentence2"),
    "wnli": ("sentence1", "sentence2"),
}
logger = get_logger(__name__)

def process(s):
    s = s.lower().replace('"', '').replace("'", "").strip()
    s = s.replace('[sep]', '[SEP]')
    return s

def process_goal(state):
    state = state.lower().replace('"', '').replace("'", "")
    state = state.replace('amazon shopping game\ninstruction:', '').replace('webshop\ninstruction:', '')
    state = state.replace('\n[button] search [button_]', '').strip()
    if ', and price lower than' in state:
        state = state.split(', and price lower than')[0]
    return state

# find the last item the agent clicked
def find_image_asin(actions, state_idx):
    for i in range(state_idx - 1, -1, -1):
        match = re.match(r'^click\[(?P<id>[a-z0-9]{10})\]', actions[i])
        if match:
            asin = match.group('id')
            if asin.isalpha(): # ASIN always contain digits
                continue
            return asin
    return "none"

def get_data(split, filter_search=True):
    print('Loading data from {}'.format(TRAJ_PATH))
    with open(TRAJ_PATH, 'r') as json_file:
        json_list = list(json_file)
    human_goals = json.load(open(GOAL_PATH, 'r'))
    
    random.seed(233)
    random.shuffle(json_list)
    
    # split by human goal index
    goal_range = range(len(human_goals))
    if split == 'train':
        goal_range = range(1500, len(human_goals))
    elif split == 'eval':
        goal_range = range(500, 1500)
    elif split == 'test':
        goal_range = range(0, 500)

    bad = cnt = 0
    state_list, action_list, idx_list, size_list = [], [], [], []
    image_list = []
    raw_image_list = []
    num_trajs = 0
    for json_str in json_list:
        result = json.loads(json_str)
        s = process_goal(result['states'][0])
        assert s in human_goals, s
        goal_idx = human_goals.index(s)
        if goal_idx not in goal_range:
            continue
        num_trajs += 1
        if 'images' not in result:
            result['images'] = [0] * len(result['states'])
        for i, (state, valid_acts, idx, image) in enumerate(zip(result['states'], result['available_actions'], result['action_idxs'], result['images'])):
            cnt += 1
            if filter_search and idx == -1:
                continue
            state_list.append(state)

            if image == 0:
                image_list.append([0.] * 512)
                raw_image_list.append("none")
            else:
                image_list.append(image)
                asin = find_image_asin(result['actions'], i) # asin = "none" if not found
                raw_image_list.append(asin)

            if len(valid_acts) > 20:  # do some action space reduction...
                bad += 1
                new_idxs = list(range(6)) + \
                    random.sample(range(6, len(valid_acts)), 10)
                if idx not in new_idxs:
                    new_idxs += [idx]
                new_idxs = sorted(new_idxs)
                valid_acts = [valid_acts[i] for i in new_idxs]
                idx = new_idxs.index(idx)
                # print(valid_acts)
            action_list.extend(valid_acts)
            idx_list.append(idx)
            size_list.append(len(valid_acts))
    print('num of {} trajs: {}'.format(split, num_trajs))
    print('total transitions and bad transitions: {} {}'.format(cnt, bad))
    state_list, action_list = list(map(process, state_list)), list(map(process, action_list))
    return state_list, action_list, idx_list, size_list, image_list, raw_image_list

def get_dataset(split, tokenizer=None):
    states, actions, idxs, sizes, images, raw_images = get_data(split)
    state_encodings = tokenizer(states, padding='max_length', max_length=512, truncation=True, return_tensors='pt')
    action_encodings = tokenizer(actions, padding='max_length', max_length=128, truncation=True, return_tensors='pt')
    dataset = {
        'state_input_ids': state_encodings['input_ids'],
        'state_attention_mask': state_encodings['attention_mask'],
        'action_input_ids': action_encodings['input_ids'].split(sizes),
        'action_attention_mask': action_encodings['attention_mask'].split(sizes),
        'sizes': sizes,
        'images': torch.tensor(images),
        'raw_images': raw_images,
        'labels': idxs,
    }
    return Dataset.from_dict(dataset)

def data_collator(batch):
    state_input_ids, state_attention_mask, action_input_ids, action_attention_mask, raw_images, sizes, labels, images = [
    ], [], [], [], [], [], [], []
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor()
    ])
    for sample in batch:
        state_input_ids.append(sample['state_input_ids'])
        state_attention_mask.append(sample['state_attention_mask'])
        action_input_ids.extend(sample['action_input_ids'])
        action_attention_mask.extend(sample['action_attention_mask'])
        
        # We load images from disk on-the-fly to save memory
        # Elements of raw_images have shape (3, H, W)
        if sample['raw_images'] == 'none':
            raw_image = torch.zeros((3, IMAGE_SIZE, IMAGE_SIZE), dtype=torch.float32) + 1
            raw_images.append(raw_image)
        else:
            asin = sample['raw_images'].upper()
            try: # just in case the image doesn't exist due to some strange reasons
                image = Image.open(os.path.join(IMAGE_PATH, asin + ".jpg"))
                image_tensor = transform(image, return_tensors="pt")['pixel_values'][0]
                raw_images.append(image_tensor)
            except:
                image_tensor = torch.zeros((3, IMAGE_SIZE, IMAGE_SIZE), dtype=torch.float32) + 1
                raw_images.append(image_tensor)
                #print("Image not found: " + os.path.join(IMAGE_PATH, asin + ".jpg"))
        
        sizes.append(sample['sizes'])
        labels.append(sample['labels'])
        images.append(sample['images'])
    
    max_state_len = max(sum(x) for x in state_attention_mask)
    max_action_len = max(sum(x) for x in action_attention_mask)

    return {
        'state_input_ids': torch.tensor(state_input_ids)[:, :max_state_len],
        'state_attention_mask': torch.tensor(state_attention_mask)[:, :max_state_len],
        'action_input_ids': torch.tensor(action_input_ids)[:, :max_action_len],
        'action_attention_mask': torch.tensor(action_attention_mask)[:, :max_action_len],
        'sizes': torch.tensor(sizes),
        'images': torch.tensor(images),
        'raw_images': torch.stack(raw_images, dim=0),
        'labels': torch.tensor(labels),
    }

def get_dataloader(split, tokenizer, shuffle=True, batch_size=1):
    return DataLoader(
        get_dataset(split), shuffle=shuffle, collate_fn=data_collator, batch_size=batch_size
    )

def parse_args():
    parser = argparse.ArgumentParser(
        description="Finetune a transformers model on a text classification task")
    parser.add_argument(
        "--task_name",
        type=str,
        default="mprc",
        help="The name of the glue task to train on.",
        choices=list(task_to_keys.keys()),
    )
    parser.add_argument(
        "--train_file", type=str, default=None, help="A csv or a json file containing the training data."
    )
    parser.add_argument(
        "--validation_file", type=str, default=None, help="A csv or a json file containing the validation data."
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=128,
        help=(
            "The maximum total input sequence length after tokenization. Sequences longer than this will be truncated,"
            " sequences shorter will be padded if `--pad_to_max_lengh` is passed."
        ),
    )
    parser.add_argument(
        "--pad_to_max_length",
        action="store_true",
        help="If passed, pad all samples to `max_length`. Otherwise, dynamic padding is used.",
    )
    parser.add_argument(
        "--model_name_or_path",
        default="bert-base-uncased",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=1,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float,
                        default=0.0, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=10,
                        help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=32,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts",
                 "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default="./ckpts/web_click",
                        help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=None,
                        help="A seed for reproducible training.")
    parser.add_argument("--push_to_hub", action="store_true",
                        help="Whether or not to push the model to the Hub.")
    parser.add_argument(
        "--hub_model_id", type=str, help="The name of the repository to keep in sync with the local `output_dir`."
    )
    parser.add_argument("--hub_token", type=str,
                        help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default="epoch",
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        type=int,
        default=1,
        help="Whether to load in all available experiment trackers from the environment and use them for logging.",
    )

    parser.add_argument("--mem", type=int, default=0, help="State with memory")
    parser.add_argument("--image", type=int, default=1,
                        help="State with image")
    parser.add_argument("--pretrain", type=int, default=1,
                        help="Pretrained BERT or not")

    parser.add_argument("--logging_steps", type=int,
                        default=10, help="Logging in training")

    parser.add_argument("--model_name", type=str, default="bert-vit", help="Name of the text encoder model (e.g. bert-base, t5-small, ...)")

    args = parser.parse_args()

    # Sanity checks
    if args.task_name is None and args.train_file is None and args.validation_file is None:
        raise ValueError(
            "Need either a task name or a training/validation file.")
    else:
        if args.train_file is not None:
            extension = args.train_file.split(".")[-1]
            assert extension in [
                "csv", "json"], "`train_file` should be a csv or a json file."
        if args.validation_file is not None:
            extension = args.validation_file.split(".")[-1]
            assert extension in [
                "csv", "json"], "`validation_file` should be a csv or a json file."

    if args.push_to_hub:
        assert args.output_dir is not None, "Need an `output_dir` to create a repo when `--push_to_hub` is passed."

    return args


def main():
    args = parse_args()

    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    # If we're using tracking, we also need to initialize it here and it will pick up all supported trackers in the environment
    # accelerator = Accelerator(log_with="wandb", logging_dir=args.output_dir) if args.with_tracking else Accelerator()
    accelerator = Accelerator()
    # Make one log on every process with the configuration for debugging.

    output_folder = os.path.join(args.output_dir, args.model_name)

    # wandb.init(project="bert_il", config=args, name=output_folder)

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    #config = BertConfigForWebshop(image=args.image, pretrain_bert=args.pretrain)

    print("Using text encoder: {}".format(args.model_name))

    tokenizer = XLMRobertaTokenizer("/home/ubuntu/Tong/WebShop/baseline_models/beit3.spm")
    model_args = _get_base_config()
    model = BEiT3ForVisualQuestionAnswering(args = model_args, num_classes = 1)
    model_weights = torch.load("/home/ubuntu/Tong/WebShop/baseline_models/beit3_base_indomain_patch16_224.pth", map_location='cpu')['model']
    model.load_state_dict(model_weights, strict = False)

    print("Text encoder loaded")
    
    train_dataset = get_dataset("train", tokenizer=tokenizer)
    eval_dataset = get_dataset("eval", tokenizer=tokenizer)

    # DataLoaders creation:
    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=data_collator, batch_size=args.per_device_train_batch_size
    )
    eval_dataloader = DataLoader(
        eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size
    )

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

    # Scheduler and math around the number of training steps.
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    else:
        args.num_train_epochs = math.ceil(
            args.max_train_steps / num_update_steps_per_epoch)

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps,
        num_training_steps=args.max_train_steps,
    )

    # Prepare everything with our `accelerator`.
    model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
    )

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / args.gradient_accumulation_steps)
    args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch

    # Figure out how many steps we should save the Accelerator states
    if hasattr(args.checkpointing_steps, "isdigit"):
        checkpointing_steps = args.checkpointing_steps
        if args.checkpointing_steps.isdigit():
            checkpointing_steps = int(args.checkpointing_steps)
    else:
        checkpointing_steps = None

    # We need to initialize the trackers we use, and also store our configuration
    if args.with_tracking:
        experiment_config = vars(args)
        # TensorBoard cannot log Enums, need the raw value
        experiment_config["lr_scheduler_type"] = experiment_config["lr_scheduler_type"].value
        accelerator.init_trackers("glue_no_trainer", experiment_config)

    # Get the metric function
    metric = load_metric("accuracy")

    # Train!
    total_batch_size = args.per_device_train_batch_size * \
        accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(
        f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(
        f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(
        f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps),
                        disable=not accelerator.is_local_main_process)
    completed_steps = 0
    starting_epoch = 0
    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            accelerator.print(
                f"Resumed from checkpoint: {args.resume_from_checkpoint}")
            accelerator.load_state(args.resume_from_checkpoint)
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            # Sorts folders by date modified, most recent checkpoint is the last
            path = dirs[-1]
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
        else:
            resume_step = int(training_difference.replace("step_", ""))
            starting_epoch = resume_step // len(train_dataloader)
            resume_step -= starting_epoch * len(train_dataloader)

    for epoch in range(starting_epoch, args.num_train_epochs):
        model.train()
        if args.with_tracking:
            total_loss = total_step = 0

        for step, batch in enumerate(train_dataloader):
            state_input_ids = batch['state_input_ids']
            state_attention_mask = batch['state_attention_mask']
            action_input_ids = batch['action_input_ids']
            action_attention_mask = batch['action_attention_mask']
            sizes = batch['sizes']
            raw_images = batch['raw_images']
            images = batch['images']
            labels = batch['labels']
            # print("State Input IDs shape:", state_input_ids.shape)
            # print("State Mask shape:", state_attention_mask.shape)
            # print("Action Input IDs shape:", action_input_ids.shape)
            # print("Action Mask shape:", action_attention_mask.shape)
            # print("Sizes shape:", sizes.shape)
            # print("Images shape:", images.shape)
            # print("Labels shape:", labels.shape)
            # break
            # We need to skip steps until we reach the resumed step
            if args.resume_from_checkpoint and epoch == starting_epoch:
                if resume_step is not None and step < resume_step:
                    completed_steps += 1
                    continue
            outputs = model(state_input_ids, state_attention_mask, action_input_ids, action_attention_mask, sizes, raw_images, labels)
            loss = outputs.loss
            # We keep track of the loss at each epoch
            if args.with_tracking:
                total_loss += loss.detach().float()
                total_step += 1
            loss = loss / args.gradient_accumulation_steps
            accelerator.backward(loss)

            metric.add_batch(
                predictions=torch.stack([logit.argmax(dim=0)
                                        for logit in outputs.logits]),
                references=batch["labels"]
            )

            if step % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()
                progress_bar.update(1)
                completed_steps += 1

                if args.with_tracking and args.logging_steps > 0 and completed_steps % args.logging_steps == 0:
                    train_metric = metric.compute()
                    # wandb.log(
                    #     {
                    #         "train_accuracy": train_metric,
                    #         "train_loss": total_loss / total_step,
                    #         "train_step": completed_steps,
                    #     },
                    # )
                    total_loss = total_step = 0

            if isinstance(checkpointing_steps, int):
                if completed_steps % checkpointing_steps == 0:
                    output_dir = f"step_{completed_steps }"
                    if output_folder is not None:
                        output_dir = os.path.join(output_folder, output_dir)
                    accelerator.save_state(output_dir)

            if completed_steps >= args.max_train_steps:
                break

        model.eval()
        samples_seen = 0
        total_loss = total_step = 0
        if len(metric) > 0:
            metric.compute()

        for step, batch in enumerate(eval_dataloader):
            state_input_ids = batch['state_input_ids']
            state_attention_mask = batch['state_attention_mask']
            action_input_ids = batch['action_input_ids']
            action_attention_mask = batch['action_attention_mask']
            sizes = batch['sizes']
            raw_images = batch['raw_images']
            images = batch['images']
            labels = batch['labels']
            with torch.no_grad():
                outputs = model(state_input_ids, state_attention_mask, action_input_ids, action_attention_mask, sizes, raw_images, labels)
            predictions = torch.stack([logit.argmax(dim=0)
                                      for logit in outputs.logits])
            predictions, references = accelerator.gather(
                (predictions, batch["labels"]))
            # If we are in a multiprocess environment, the last batch has duplicates
            if accelerator.num_processes > 1:
                if step == len(eval_dataloader):
                    predictions = predictions[: len(
                        eval_dataloader.dataset) - samples_seen]
                    references = references[: len(
                        eval_dataloader.dataset) - samples_seen]
                else:
                    samples_seen += references.shape[0]
            metric.add_batch(
                predictions=predictions,
                references=references,
            )
            total_loss += outputs.loss.detach().float()
            total_step += 1

        eval_metric = metric.compute()
        logger.info(f"epoch {epoch}: {eval_metric}")

        # if args.with_tracking:
        #     wandb.log(
        #         {
        #             "eval_accuracy": eval_metric,
        #             "eval_loss": total_loss / total_step,
        #             "epoch": epoch,
        #             "epoch_step": completed_steps,
        #         },
        #     )

        if args.checkpointing_steps == "epoch":
            output_dir = f"epoch_{epoch}"
            if output_folder is not None:
                output_dir = os.path.join(output_folder, output_dir)
            os.makedirs(output_dir, exist_ok=True)
            unwrapped_model = accelerator.unwrap_model(model)
            torch.save(unwrapped_model.state_dict(),
                       os.path.join(output_dir, "model.pth"))

            # accelerator.save_state(output_dir)

    if output_folder is not None:
        with open(os.path.join(output_folder, "all_results.json"), "w") as f:
            json.dump({"eval_accuracy": eval_metric["accuracy"]}, f)


if __name__ == "__main__":
    main()
