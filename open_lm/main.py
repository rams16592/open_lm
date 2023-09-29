import glob
import logging
import os
import re
import subprocess
import sys
import random
from datetime import datetime
import functools
import numpy as np
from functools import partial
from pathlib import Path
import json

import torch
from torch import optim
from torch.cuda.amp import GradScaler

import torch.distributed as dist

from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    BackwardPrefetch,
    ShardingStrategy,
    FullStateDictConfig,
    StateDictType,
    CPUOffload,
    ShardingStrategy,
)
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy

from open_lm.model import Block
from open_lm.losses import CrossEntropyLossWithZLoss

try:
    import wandb
except ImportError:
    wandb = None

try:
    import torch.utils.tensorboard as tensorboard
except ImportError:
    tensorboard = None

from open_lm.model import create_model
from open_lm.data import get_data, get_wds_dataset
from open_lm.distributed import is_master, init_distributed_device, broadcast_object
from open_lm.logger import setup_logging
from open_lm.params import parse_args
from open_lm.scheduler import cosine_lr
from open_lm.train import train_one_epoch, evaluate
from open_lm.file_utils import (
    pt_load,
    check_exists,
    start_sync_process,
    remote_sync,
    get_string_for_epoch,
)


LATEST_CHECKPOINT_NAME = "epoch_latest.pt"


def random_seed(seed=42, rank=0):
    torch.manual_seed(seed + rank)
    np.random.seed(seed + rank)
    random.seed(seed + rank)


def natural_key(string_):
    """See http://www.codinghorror.com/blog/archives/001018.html"""
    return [int(s) if s.isdigit() else s for s in re.split(r"(\d+)", string_.lower())]


def get_latest_checkpoint(path: str, remote: bool):
    # as writen, this glob recurses, so can pick up checkpoints across multiple sub-folders
    if remote:
        result = subprocess.run(
            ["aws", "s3", "ls", path + "/"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        print(result)
        if result.returncode == 1:
            return None
        checkpoints = [
            os.path.join(path, x.split(" ")[-1])
            for x in result.stdout.decode().split("\n")[:-1]
        ]
    else:
        checkpoints = glob.glob(path + "**/epoch_*.pt", recursive=True)
    if checkpoints:
        checkpoints = sorted(checkpoints, key=natural_key)
        return checkpoints[-1]
    return None


def get_state_dict(name):
    checkpoint = pt_load(name, map_location="cpu")
    if "epoch" in checkpoint:
        sd = checkpoint["state_dict"]
        if next(iter(sd.items()))[0].startswith("module"):
            sd = {k[len("module.") :]: v for k, v in sd.items()}
    else:
        sd = checkpoint
    return sd


def load_model(args, model):
    checkpoint = pt_load(args.resume, map_location="cpu")
    if "epoch" in checkpoint:
        # resuming a train checkpoint w/ epoch and optimizer state
        start_epoch = checkpoint["epoch"]
        sd = checkpoint["state_dict"]
        if next(iter(sd.items()))[0].startswith("module"):
            sd = {k[len("module.") :]: v for k, v in sd.items()}
        model.load_state_dict(sd)
        logging.info(f"=> resuming checkpoint '{args.resume}' (epoch {start_epoch})")
    else:
        # loading a bare (model only) checkpoint for fine-tune or evaluation
        model.load_state_dict(checkpoint)
        logging.info(f"=> loaded checkpoint '{args.resume}' (epoch {start_epoch})")
    return start_epoch


def load_optimizer(args, model, optimizer, scaler):
    potential_checkpoint = args.resume.replace("epoch_", "optimizer_")
    if check_exists(potential_checkpoint):
        checkpoint = pt_load(potential_checkpoint, map_location="cpu")
    else:
        checkpoint = pt_load(args.resume, map_location="cpu")
    if "optimizer" in checkpoint:
        if optimizer is not None:
            osd = checkpoint["optimizer"]
            if args.fsdp:
                osd = FSDP.optim_state_dict_to_load(osd, model, optimizer)
            optimizer.load_state_dict(osd)
            logging.info(f"=> resuming optimizer")
        if scaler is not None and "scaler" in checkpoint:
            scaler.load_state_dict(checkpoint["scaler"])
    else:
        logging.info(f"=> WARNING: not resuming optimizer.")


def save_checkpoint(args, model, optimizer, scaler, completed_epoch, evaluation_loss):
    cpu_state, optim_state = None, None
    if args.logs and args.logs.lower() != "none" and args.fsdp:
        save_policy = FullStateDictConfig(offload_to_cpu=True, rank0_only=True)
        with FSDP.state_dict_type(model, StateDictType.FULL_STATE_DICT, save_policy):
            cpu_state = model.state_dict()
            optim_state = FSDP.optim_state_dict(model, optimizer)

    if args.save_logs:
        checkpoint_dict_model = {
            "epoch": completed_epoch,
            "name": args.name,
            "state_dict": cpu_state if args.fsdp else model.state_dict(),
            "evaluation_loss": evaluation_loss,
        }
        checkpoint_dict_opt = {
            "epoch": completed_epoch,
            "name": args.name,
            "optimizer": optim_state if args.fsdp else optimizer.state_dict(),
            "evaluation_loss": evaluation_loss,
        }
        if scaler is not None:
            checkpoint_dict_opt["scaler"] = scaler.state_dict()

        if completed_epoch == args.epochs or (
            args.save_frequency > 0 and (completed_epoch % args.save_frequency) == 0
        ):
            torch.save(
                checkpoint_dict_model,
                os.path.join(args.checkpoint_path, f"epoch_{completed_epoch}.pt"),
            )
            torch.save(
                checkpoint_dict_opt,
                os.path.join(args.checkpoint_path, f"optimizer_{completed_epoch}.pt"),
            )

        if args.delete_previous_checkpoint:
            previous_checkpoint = os.path.join(
                args.checkpoint_path, f"epoch_{completed_epoch - 1}.pt"
            )
            if os.path.exists(previous_checkpoint):
                os.remove(previous_checkpoint)
            previous_checkpoint = os.path.join(
                args.checkpoint_path, f"optimizer_{completed_epoch - 1}.pt"
            )
            if os.path.exists(previous_checkpoint):
                os.remove(previous_checkpoint)


def main(args):
    args = parse_args(args)

    if torch.cuda.is_available():
        # This enables tf32 on Ampere GPUs which is only 8% slower than
        # float16 and almost as accurate as float32
        # This was a default in pytorch until 1.12
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = False

    # fully initialize distributed device environment
    device = init_distributed_device(args)

    # get the name of the experiments
    if args.name is None:
        # sanitize model name for filesystem / uri use, easier if we don't use / in name as a rule?
        model_name_safe = args.model.replace("/", "-")
        date_str = datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
        if args.distributed:
            # sync date_str from master to all ranks
            date_str = broadcast_object(args, date_str)
        args.name = "-".join(
            [
                date_str,
                f"model_{model_name_safe}",
                f"lr_{args.lr}",
                f"b_{args.batch_size}",
            ]
        )

    resume_latest = args.resume == "latest"
    log_base_path = os.path.join(args.logs, args.name)
    args.log_path = None
    if is_master(args, local=args.log_local):
        os.makedirs(log_base_path, exist_ok=True)
        log_filename = f"out-{args.rank}" if args.log_local else "out.log"
        args.log_path = os.path.join(log_base_path, log_filename)
        if os.path.exists(args.log_path) and not resume_latest:
            print(
                "Error. Experiment already exists. Use --name {} to specify a new experiment."
            )
            return -1

    # Setup text logger
    args.log_level = logging.DEBUG if args.debug else logging.INFO
    setup_logging(args.log_path, args.log_level)

    # Setup wandb, tensorboard, checkpoint logging
    args.wandb = "wandb" in args.report_to or "all" in args.report_to
    args.tensorboard = "tensorboard" in args.report_to or "all" in args.report_to
    args.checkpoint_path = os.path.join(log_base_path, "checkpoints")
    if is_master(args):
        args.tensorboard_path = (
            os.path.join(log_base_path, "tensorboard") if args.tensorboard else ""
        )
        for dirname in [args.tensorboard_path, args.checkpoint_path]:
            if dirname:
                os.makedirs(dirname, exist_ok=True)
    else:
        args.tensorboard_path = ""

    if resume_latest:
        resume_from = None
        checkpoint_path = args.checkpoint_path
        # If using remote_sync, need to check the remote instead of the local checkpoints folder.
        if args.remote_sync is not None:
            checkpoint_path = os.path.join(args.remote_sync, args.name, "checkpoints")
            if args.save_most_recent:
                print(
                    "Error. Cannot use save-most-recent with remote_sync and resume latest."
                )
                return -1
            if args.remote_sync_protocol != "s3":
                print("Error. Sync protocol not supported when using resume latest.")
                return -1
        if is_master(args):
            # Checking for existing checkpoint via master rank only. It is possible for
            # different rank processes to see different files if a shared file-system is under
            # stress, however it's very difficult to fully work around such situations.
            if args.save_most_recent:
                # if --save-most-recent flag is set, look for latest at a fixed filename
                resume_from = os.path.join(checkpoint_path, LATEST_CHECKPOINT_NAME)
                if not os.path.exists(resume_from):
                    # If no latest checkpoint has been saved yet, don't try to resume
                    resume_from = None
            else:
                # otherwise, list checkpoint dir contents and pick the newest checkpoint
                resume_from = get_latest_checkpoint(
                    checkpoint_path, remote=args.remote_sync is not None
                )
            if resume_from:
                logging.info(f"Found latest resume checkpoint at {resume_from}.")
            else:
                logging.info(f"No latest resume checkpoint found in {checkpoint_path}.")
        if args.distributed:
            # sync found checkpoint path to all ranks
            resume_from = broadcast_object(args, resume_from)
        args.resume = resume_from

    if args.copy_codebase:
        copy_codebase(args)

    # start the sync proces if remote-sync is not None
    remote_sync_process = None
    if is_master(args) and args.remote_sync is not None:
        # first make sure it works
        result = remote_sync(
            os.path.join(args.logs, args.name),
            os.path.join(args.remote_sync, args.name),
            args.remote_sync_protocol,
        )
        if result:
            logging.info("remote sync successful.")
        else:
            logging.info("Error: remote sync failed. Exiting.")
            return -1
        # if all looks good, start a process to do this every args.remote_sync_frequency seconds
        remote_sync_process = start_sync_process(
            args.remote_sync_frequency,
            os.path.join(args.logs, args.name),
            os.path.join(args.remote_sync, args.name),
            args.remote_sync_protocol,
        )
        remote_sync_process.start()

    if args.precision == "fp16":
        logging.warning(
            "It is recommended to use AMP mixed-precision instead of FP16. "
            "FP16 support needs further verification and tuning, especially for train."
        )

    elif args.distributed:
        logging.info(
            f"Running in distributed mode with multiple processes. Device: {args.device}."
            f"Process (global: {args.rank}, local {args.local_rank}), total {args.world_size}."
        )
    else:
        logging.info(f"Running with a single process. Device {args.device}.")

    random_seed(args.seed, 0)
    model = create_model(args)
    args.vocab_size = model.vocab_size
    args.seq_len = model.seq_len
    if args.train_num_samples is not None:
        args.train_num_samples //= args.seq_len
    model = model.to(device)

    random_seed(args.seed, args.rank)

    if args.grad_checkpointing:
        model.set_grad_checkpointing()

    if is_master(args):
        logging.info("Model:")
        logging.info(f"{str(model)}")
        logging.info("Params:")
        params_file = os.path.join(args.logs, args.name, "params.txt")
        with open(params_file, "w") as f:
            for name in sorted(vars(args)):
                val = getattr(args, name)
                logging.info(f"  {name}: {val}")
                f.write(f"{name}: {val}\n")

    # optionally resume model from a checkpoint
    start_epoch = 0
    if args.resume is not None:
        start_epoch = load_model(args, model)
    elif args.pretrained is not None:
        print("=> loading from a pre-trained model.")
        args.resume = args.pretrained
        ep = load_model(args, model)
        # this flag continues training from the pre-trained model.
        if args.load_pretrained_state:
            start_epoch = ep
        else:
            args.resume = None
    elif args.average is not None:
        num_models_to_average = len(args.average)
        print(
            "=> Averaging models: ",
            args.average,
            " with coefficients: ",
            args.average_coefficients,
        )
        assert (
            num_models_to_average > 1
        ), "num_models_to_average must be > 1 - else use --pretrained"
        if args.average_coefficients is None:
            args.average_coefficients = [
                1.0 / num_models_to_average
            ] * num_models_to_average
        else:
            assert len(args.average_coefficients) == num_models_to_average
        state_dict = {
            k: v * args.average_coefficients[0]
            for k, v in get_state_dict(args.average[0]).items()
        }
        for i in range(1, num_models_to_average):
            state_dict_i = get_state_dict(args.average[i])
            for k in state_dict:
                state_dict[k] = (
                    state_dict[k] + state_dict_i[k] * args.average_coefficients[i]
                )
        model.load_state_dict(state_dict)

    if args.distributed:
        if args.fsdp:
            # from https://pytorch.org/blog/efficient-large-scale-training-with-pytorch/
            transformer_auto_wrapper_policy = functools.partial(
                transformer_auto_wrap_policy,
                transformer_layer_cls={
                    Block,
                },
            )
            # tries to follow gopher...
            mp_policy = None
            if args.fsdp_amp:
                print("=> using bfloat16 params as part of fsdp amp policy.")
                mp_policy = MixedPrecision(
                    param_dtype=torch.bfloat16,
                    reduce_dtype=torch.float32,
                    buffer_dtype=torch.bfloat16,
                )

            if args.rank == 0:
                print(
                    f"Before FSDP parameter num: {sum(p.numel() for p in model.parameters())}"
                )
                print(f"Before FSDP {torch.cuda.memory_allocated()/1024**3:.3} GB")

            fsdp_kwargs = {}
            assert not (args.fsdp_hybrid and args.fsdp_hybrid_o2), "Only --fsdp-hybrid or --fsdp-hybrid-o2 should be set."
            if args.fsdp_backward_prefetch:
                fsdp_kwargs["backward_prefetch"] = BackwardPrefetch.BACKWARD_PRE
            if args.fsdp_hybrid:
                fsdp_kwargs["sharding_strategy"] = ShardingStrategy.HYBRID_SHARD
            if args.fsdp_hybrid_o2:
                fsdp_kwargs["sharding_strategy"] = ShardingStrategy._HYBRID_SHARD_ZERO2
            print("=> FSDP kwargs: ", fsdp_kwargs)

            # init FSDP
            model = FSDP(
                model,
                auto_wrap_policy=transformer_auto_wrapper_policy,
                device_id=device,
                mixed_precision=mp_policy,
                cpu_offload=CPUOffload(offload_params=args.fsdp_cpu_offload),
                use_orig_params=args.fsdp_use_orig_params,
                limit_all_gathers=args.fsdp_limit_all_gathers,
                **fsdp_kwargs,
            )

            print(
                f"After FSDP parameter num: {sum(p.numel() for p in model.parameters())} on rank {args.rank}"
            )
            print(
                f"After FSDP {torch.cuda.memory_allocated()/1024**3:.3} GB on rank {args.rank}"
            )
        else:
            ddp_args = {}
            if args.ddp_static_graph:
                # this doesn't exist in older PyTorch, arg only added if enabled
                ddp_args["static_graph"] = True
            model = torch.nn.parallel.DistributedDataParallel(
                model, device_ids=[device], **ddp_args
            )

    # create optimizer and scaler
    optimizer = None
    scaler = None

    if args.train_data or (args.dataset_metadata is not None):
        named_parameters = list(model.named_parameters())
        no_decay_params = []  # to be potentially used later
        params = [p for n, p in named_parameters if p.requires_grad]

        optimizer = optim.AdamW(
            [
                {"params": no_decay_params, "weight_decay": 0.0},
                {"params": params, "weight_decay": args.wd},
            ],
            lr=args.lr,
            betas=(args.beta1, args.beta2),
            eps=args.eps,
        )
        scaler = None
        if args.precision == "amp":
            assert not args.fsdp, "FSDP not supported with amp, only amp_bfloat16"
            scaler = GradScaler()

    # optionally resume optimizer from a checkpoint
    if args.resume is not None:
        load_optimizer(args, model, optimizer, scaler)

    # initialize datasets
    # use tokenizer=None because the data is already pre-tokenized.
    if args.val_data is not None:
        args.val_data = [args.val_data]
    data = get_data(
        args,
        epoch=start_epoch,
        tokenizer=None,
        skip_train=args.dataset_metadata is not None,
    )

    if args.torchcompile:
        logging.info("Compiling model...")
        model = torch.compile(model)

    # create scheduler if train
    scheduler = None
    if "train" in data and optimizer is not None:
        if args.dataset_metadata is not None:
            total_steps = (args.train_num_samples * args.epochs) // (
                args.batch_size * args.world_size
            )
        else:
            total_steps = (data["train"].dataloader.num_batches) * args.epochs

        if args.lr_scheduler == "cosine":
            scheduler = cosine_lr(
                optimizer,
                args.lr,
                args.warmup,
                total_steps,
                args.lr_cooldown_end,
                args.force_min_lr,
            )
        else:
            logging.error(
                f"Unknown scheduler, {args.lr_scheduler}. Available options are: cosine, const, const-cooldown."
            )
            exit(1)

    # determine if this worker should save logs and checkpoints. only do so if it is rank == 0
    args.save_logs = args.logs and args.logs.lower() != "none" and is_master(args)
    writer = None
    if args.save_logs and args.tensorboard:
        assert tensorboard is not None, "Please install tensorboard."
        writer = tensorboard.SummaryWriter(args.tensorboard_path)
    if args.wandb and is_master(args):
        assert wandb is not None, "Please install wandb."
        logging.debug("Starting wandb.")
        if args.val_data is not None:
            args.val_sz = data["val"].dataloader.num_samples
        wandb.init(
            project=args.wandb_project_name,
            name=args.name,
            notes=args.wandb_notes,
            tags=[],
            resume=None,
            config=vars(args),
        )
        if args.debug:
            wandb.watch(model, log="all")
        wandb.save(params_file)
        logging.debug("Finished loading wandb.")

    if "train" not in data:
        checkpoint_root = Path(args.resume).parent

        metrics = evaluate(model, data, start_epoch, args, writer)
        metrics["checkpoint_path"] = args.resume
        metrics["val_data"] = args.val_data
        metrics["model"] = args.model

        if is_master(args):
            with open(os.path.join(checkpoint_root, "results.jsonl"), "a+") as f:
                f.write(json.dumps(metrics))
                f.write("\n")

        return

    loss = torch.nn.CrossEntropyLoss()
    if args.z_loss_coefficient != 0.0:
        if is_master(args):
            logging.info("Using CrossEntropyLossWithZLoss.")
        loss = CrossEntropyLossWithZLoss(args.z_loss_coefficient)

    for epoch in range(start_epoch, args.epochs):
        if is_master(args):
            logging.info(f"Start epoch {epoch}")

        if args.dataset_metadata is not None:
            assert (
                not args.dataset_resampled
            ), "dataset_metadata and dataset_resampled are mutually exclusive"
            train_data_string, num_samples = get_string_for_epoch(
                args.train_num_samples, epoch, args.dataset_metadata
            )
            print(f"=> epoch {epoch}, training on {train_data_string}")
            if data["train"] is not None:
                del data["train"]
            args.train_data = train_data_string
            data["train"] = get_wds_dataset(
                args, True, epoch, force_num_samples=num_samples
            )
        if args.distributed:
            dist.barrier()

        success = train_one_epoch(
                model,
                data,
                loss,
                epoch,
                optimizer,
                scaler,
                scheduler,
                args,
                tb_writer=writer,
            )

        if args.distributed:
            dist.barrier()

        if not success:
            logging.info(f"Training exiting due to NaN value")
            break

        completed_epoch = epoch + 1
        evaluation_loss = -1
        if "val" in data:
            evaluation_loss = evaluate(model, data, completed_epoch, args, writer)[
                "loss"
            ]

        # 613 - 610 at halfway
        # Saving checkpoints.
        save_checkpoint(
            args, model, optimizer, scaler, completed_epoch, evaluation_loss
        )

    if args.wandb and is_master(args):
        wandb.finish()

    # run a final sync.
    if remote_sync_process is not None:
        logging.info("Final remote sync.")
        remote_sync_process.terminate()
        result = remote_sync(
            os.path.join(args.logs, args.name),
            os.path.join(args.remote_sync, args.name),
            args.remote_sync_protocol,
        )
        if result:
            logging.info("Final remote sync successful.")
        else:
            logging.info("Final remote sync failed.")


def copy_codebase(args):
    from shutil import copytree, ignore_patterns

    new_code_path = os.path.join(args.logs, args.name, "code")
    if os.path.exists(new_code_path):
        print(
            f"Error. Experiment already exists at {new_code_path}. Use --name to specify a new experiment."
        )
        return -1
    print(f"Copying codebase to {new_code_path}")
    current_code_path = os.path.realpath(__file__)
    for _ in range(3):
        current_code_path = os.path.dirname(current_code_path)
    copytree(
        current_code_path, new_code_path, ignore=ignore_patterns("log", "logs", "wandb")
    )
    print("Done copying code.")
    return 1


if __name__ == "__main__":
    main(sys.argv[1:])
