import argparse
import ast


def get_default_params(model_name):
    model_name = model_name.lower()
    if "vit" in model_name:
        return {"lr": 5.0e-4, "beta1": 0.9, "beta2": 0.98, "eps": 1.0e-6}
    else:
        return {"lr": 5.0e-4, "beta1": 0.9, "beta2": 0.999, "eps": 1.0e-8}


def parse_bool(arg):
    """Parse string to boolean.
    Using type=bool in argparse does not do the right thing. E.g.
    '--bool_flag False' will parse as True. See
    <https://stackoverflow.com/q/15008758/1291812>
    """
    if arg == 'True':
        return True
    elif arg == 'False':
        return False
    else:
        raise argparse.ArgumentTypeError("Expected 'True' or 'False'.")


class ParseKwargs(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        kw = {}
        for value in values:
            key, value = value.split('=')
            try:
                kw[key] = ast.literal_eval(value)
            except ValueError:
                kw[key] = str(value)  # fallback to string (avoid need to escape on command line)
        setattr(namespace, self.dest, kw)


def add_model_args(parser):
    """Add arguments that change the underlying architecture.

    These arguments need to be added to the eval code. Ideally, these should be moved to our model configs when we make
    a backward-incompatible release."""
    parser.add_argument(
        "--model-norm",
        type=str,
        default="default_layer_norm",
        choices=["default_layer_norm", "lp_layer_norm", "gain_only_layer_norm", "no_wb_layer_norm", "rms_norm"],
        help="Type of normalization to employ in the model",
    )
    parser.add_argument("--ffn-type", choices=["swiglu", "gelu"], default="swiglu")
    parser.add_argument(
        "--qk-norm",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="apply --model-norm to qk as in: https://arxiv.org/abs/2302.05442"
    )
    parser.add_argument(
        "--rotary-old",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Use incorrect rotary embedding that is applied to the head dimension, which is default in xformers as of 09/01/23."
    )


def parse_args(args):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--train-data",
        type=str,
        help="Path to file(s) with training data. When using webdataset, multiple datasources can be combined using the `::` separator.",
    )
    parser.add_argument(
        "--train-data-mix-weights",
        type=str,
        help=(
            "When using multiple data sources with webdataset and sampling with replacement, this can be used to upsample specific data sources. "
            "Similar to --train-data, this should be a string with as many numbers as there are data sources, separated by `::` (e.g. 1::2::0.5) "
            "By default, datapoints are sampled uniformly regardless of the dataset sizes."
        )
    )
    parser.add_argument(
        "--train-data-upsampling-factors",
        type=str,
        default=None,
        help=(
            "When using multiple data sources with webdataset and sampling with replacement, this can be used to upsample specific data sources. "
            "Similar to --train-data, this should be a string with as many numbers as there are data sources, separated by `::` (e.g. 1::2::0.5) "
            "By default, datapoints are sampled uniformly regardless of the dataset sizes."
        )
    )
    parser.add_argument(
        "--val-data",
        type=str,
        default=None,
        help="Path to file(s) with validation data",
    )
    parser.add_argument(
        "--data-key",
        type=str,
        default='txt',
        help="what is the extension",
    )
    parser.add_argument(
        "--train-num-samples",
        type=int,
        default=None,
        help="Number of samples in dataset. Required for webdataset if not available in info file.",
    )
    parser.add_argument(
        "--val-num-samples",
        type=int,
        default=None,
        help="Number of samples in dataset. Useful for webdataset if not available in info file.",
    )
    parser.add_argument(
        "--dataset-type",
        choices=["webdataset", "auto"],
        default="auto",
        help="Which type of dataset to process."
    )
    parser.add_argument(
        "--dataset-resampled",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Whether to use sampling with replacement for webdataset shard selection."
    )
    parser.add_argument(
        "--dataset-metadata",
        default=None,
        help="Uses metadata to construct a train set."
    )
    parser.add_argument(
        "--disable-buffer",
        type=parse_bool,
        choices=[True, False],
        default=False,
        help="Turns off the shuffle buffer."
    )
    parser.add_argument(
        "--logs",
        type=str,
        default="./logs/",
        help="Where to store tensorboard logs. Use None to avoid storing logs.",
    )
    parser.add_argument(
        "--log-local",
        type=parse_bool,
        choices=[True, False],
        default=False,
        help="log files on local master, otherwise global master only.",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Optional identifier for the experiment when storing logs. Otherwise use current time.",
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="Number of dataloader workers per GPU."
    )
    parser.add_argument(
        "--batch-size", type=int, default=64, help="Batch size per GPU."
    )
    parser.add_argument(
        "--epochs", type=int, default=32, help="Number of epochs to train for."
    )
    parser.add_argument(
        "--epochs-cooldown", type=int, default=None,
        help="When scheduler w/ cooldown used, perform cooldown from total_epochs - cooldown_epochs onwards."
    )
    parser.add_argument("--optimizer", default='adamw', help="Optimizer.")
    parser.add_argument("--lr", type=float, default=None, help="Learning rate.")
    parser.add_argument("--beta1", type=float, default=None, help="Adam beta 1.")
    parser.add_argument("--beta2", type=float, default=None, help="Adam beta 2.")
    parser.add_argument("--eps", type=float, default=None, help="Adam epsilon.")
    parser.add_argument("--wd", type=float, default=0.2, help="Weight decay.")
    parser.add_argument(
        "--warmup", type=int, default=10000, help="Number of steps to warmup for."
    )
    parser.add_argument(
        "--fused-xent",
        type=parse_bool,
        choices=[True, False],
        default=False,
        help="Whether to use fused cross entropy"
    )
    parser.add_argument(
        "--z-loss-coefficient",
        type=float,
        default=0.0,
        help="regularization term to make sure logits not too big, based on: https://github.com/google-research/t5x/blob/main/t5x/losses.py#L33-L38"
    )
    parser.add_argument(
        "--log-logit-mean",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Whether to log the logit mean to wandb etc."
    )
    parser.add_argument(
        "--use-bn-sync",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Whether to use batch norm sync.")
    parser.add_argument(
        "--skip-scheduler",
        type=parse_bool,
        choices=[True, False],
        default=False,
        help="Use this flag to skip the learning rate decay.",
    )
    parser.add_argument(
        "--lr-scheduler",
        type=str,
        default='cosine',
        help="LR scheduler. One of: 'cosine', 'const' (constant), 'const-cooldown' (constant w/ cooldown). Default: cosine",
    )
    parser.add_argument(
        "--lr-cooldown-end", type=float, default=0.0,
        help="End learning rate for cooldown schedule. Default: 0"
    )
    parser.add_argument(
        "--lr-cooldown-power", type=float, default=1.0,
        help="Power for polynomial cooldown schedule. Default: 1.0 (linear decay)"
    )
    parser.add_argument(
        "--force-min-lr", type=float, default=0.0,
        help="Force the LR to stop decaying at this value."
    )
    parser.add_argument(
        "--save-frequency", type=int, default=1, help="How often to save checkpoints."
    )
    parser.add_argument(
        "--save-most-recent",
        type=parse_bool,
        choices=[True, False],
        default=False,
        help="Always save the most recent model trained to epoch_latest.pt.",
    )
    parser.add_argument(
        "--torchcompile",
        type=parse_bool,
        choices=[True, False],
        default=False,
        help="Compile the model, requires torch >=2.0.",
    )
    parser.add_argument(
        "--val-frequency", type=int, default=1, help="How often to run evaluation with val data."
    )
    parser.add_argument(
        "--resume",
        default=None,
        type=str,
        help="path to latest checkpoint (default: none)",
    )
    parser.add_argument(
        "--precision",
        choices=["amp", "amp_bf16", "amp_bfloat16", "bf16", "fp16", "fp32"],
        default="amp",
        help="Floating point precision."
    )
    parser.add_argument(
        "--model",
        type=str,
        default="m1b_neox",
        help="Name of the vision backbone to use.",
    )
    parser.add_argument(
        "--pretrained",
        default=None,
        type=str,
        help="Use a pretrained CLIP model weights with the specified tag or file path.",
    )
    parser.add_argument(
        "--load-pretrained-state",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Include the opt and schedule state when loading a pre-trained model.",
    )
    parser.add_argument(
        "--grad-checkpointing",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Enable gradient checkpointing.",
    )
    parser.add_argument(
        "--torchscript",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="torch.jit.script the model",
    )
    parser.add_argument(
        "--trace",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="torch.jit.trace the model for inference / eval only",
    )
    parser.add_argument(
        "--accum-freq", type=int, default=1, help="Update the model every --acum-freq steps."
    )
    # arguments for distributed training
    parser.add_argument(
        "--dist-url",
        default="env://",
        type=str,
        help="url used to set up distributed training",
    )
    parser.add_argument(
        "--dist-backend", default="nccl", type=str, help="distributed backend"
    )
    parser.add_argument(
        "--fsdp",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Use FullyShardedDataParallel for distributed training."
    )
    parser.add_argument(
        "--fsdp-cpu-offload",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="CPU offloading for FSDP and checkpoint saving. This does not work with gradient accumulation."
    )
    parser.add_argument(
        "--fsdp-use-orig-params",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Passed into the FSDP constructor. This does not work for OPT models. Enables param_groups for weight_decay."
    )
    parser.add_argument(
        "--fsdp-pure-bf16",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Use pure bf16 FullyShardedDataParallel for distributed training."
    )
    parser.add_argument(
        "--fsdp-amp",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Use FullyShardedDataParallel for distributed training."
    )
    parser.add_argument(
        "--fsdp-backward-prefetch",
        default=False,
        type=parse_bool,
        choices=[True, False],
    )
    parser.add_argument(
        "--fsdp-hybrid",
        default=False,
        type=parse_bool,
        choices=[True, False],
    )
    parser.add_argument(
        "--fsdp-hybrid-o2",
        default=False,
        action="store_true",
    )
    parser.add_argument(
        "--fsdp-checkpoint",
        default=False,
        type=parse_bool,
        choices=[True, False],
    )
    parser.add_argument(
        "--fsdp-limit-all-gathers",
        default=False,
        type=parse_bool,
        choices=[True, False],
    )
    parser.add_argument(
        "--report-to",
        default='',
        type=str,
        help="Options are ['wandb', 'tensorboard', 'wandb,tensorboard']"
    )
    parser.add_argument(
        "--wandb-notes",
        default='',
        type=str,
        help="Notes if logging with wandb"
    )
    parser.add_argument(
        "--wandb-project-name",
        type=str,
        default='open-lm',
        help="Name of the project if logging with wandb.",
    )
    parser.add_argument(
        "--debug",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="If true, more information is logged."
    )
    parser.add_argument(
        "--average",
        type=str,
        nargs="+",
        default=None,
        help=(
            "Apply model average on these checkpoints with the specified coefficients by --average-coefficients."
        )
    )
    parser.add_argument(
        "--average-coefficients",
        type=float,
        nargs="+",
        default=None,
        help=(
            "Average the model weights with the specified coefficients, model weights specified by --average."
        )
    )
    parser.add_argument(
        "--copy-codebase",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="If true, we copy the entire base on the log directory, and execute from there."
    )
    parser.add_argument(
        "--ddp-static-graph",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Enable static graph optimization for DDP in PyTorch >= 1.11.",
    )
    parser.add_argument(
        "--no-set-device-rank",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="Don't set device index from local rank (when CUDA_VISIBLE_DEVICES restricted to one per proc)."
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="Default random seed."
    )
    parser.add_argument(
        "--grad-clip-norm", type=float, default=None, help="Gradient clip."
    )
    parser.add_argument(
        "--log-every-n-steps",
        type=int,
        default=100,
        help="Log every n steps to tensorboard/console/wandb.",
    )
    parser.add_argument(
        "--remote-sync",
        type=str,
        default=None,
        help="Optinoally sync with a remote path specified by this arg",
    )
    parser.add_argument(
        "--remote-sync-frequency",
        type=int,
        default=300,
        help="How frequently to sync to a remote directly if --remote-sync is not None.",
    )
    parser.add_argument(
        "--remote-sync-protocol",
        choices=["s3", "fsspec"],
        default="s3",
        help="How to do the remote sync backup if --remote-sync is not None.",
    )
    parser.add_argument(
        "--delete-previous-checkpoint",
        default=False,
        type=parse_bool,
        choices=[True, False],
        help="If true, delete previous checkpoint after storing a new one."
    )
    parser.add_argument(
        "--distill-model",
        default=None,
        help='Which model arch to distill from, if any.'
    )
    parser.add_argument(
        "--distill-pretrained",
        default=None,
        help='Which pre-trained weights to distill from, if any.'
    )
    parser.add_argument(
        "--use-bnb-linear",
        default=None,
        help='Replace the network linear layers from the bitsandbytes library. '
        'Allows int8 training/inference, etc.'
    )
    add_model_args(parser)
    args = parser.parse_args(args)

    # If some params are not passed, we use the default values based on model name.
    default_params = get_default_params(args.model)
    for name, val in default_params.items():
        if getattr(args, name) is None:
            setattr(args, name, val)

    if args.train_data == "openlm_mix_tri_s3":
        args.train_data = [
            "pipe:aws s3 cp s3://tri-ml-datasets/openlm/data/rpj_tokenized_upsampled_eleutherai/shard_{00000000..00099998}.tar -",
            "pipe:aws s3 cp s3://tri-ml-datasets/openlm/data/2T_no_rpj_tokenized_upsampled_25k_shard/shard_{00000000..00024998}.tar -"
        ]
    if args.train_data_mix_weights is not None:
        args.train_data_mix_weights = [float(x) for x in args.train_data_mix_weights.split("::")]

    return args
