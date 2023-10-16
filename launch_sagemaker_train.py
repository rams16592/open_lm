import argparse
import time
import os
import subprocess
import yaml
from datetime import datetime
from pathlib import Path

import boto3
import sagemaker
from sagemaker import get_execution_role
from sagemaker.pytorch import PyTorch
from sagemaker.inputs import TrainingInput
from sagemaker_ssh_helper.wrapper import SSHEstimatorWrapper


NAME = "openlm-speedup"


def run_command(command):
    subprocess.run(command, shell=True, check=True)


def get_image(user, build_image=False, update_image=False):
    os.environ['AWS_PROFILE'] = 'poweruser'
    account = subprocess.getoutput("aws --profile poweruser sts get-caller-identity --query Account --output text")
    algorithm_name = f"{user}-{NAME}"
    region = 'us-east-1'
    fullname = f"{account}.dkr.ecr.{region}.amazonaws.com/{algorithm_name}:latest"
    if not build_image and not update_image:
        return fullname

    login_cmd = f"aws ecr get-login-password --region {region} --profile poweruser | docker login --username AWS --password-stdin"

    if build_image:
        print("Building container")
        commands = [
            f"{login_cmd} 763104351884.dkr.ecr.us-east-1.amazonaws.com",
            f"docker build -t {algorithm_name} .",
            f"docker tag {algorithm_name} {fullname}",
            f"{login_cmd} {fullname}",
            f"aws ecr describe-repositories --repository-names {algorithm_name} || aws ecr create-repository --repository-name {algorithm_name}"
        ]
    elif update_image:
        print("Updating container")
        commands = [
            f"docker build -f update.dockerfile --build-arg BASE_DOCKER={algorithm_name} -t {algorithm_name} .",
            f"docker tag {algorithm_name} {fullname}",
            f"{login_cmd} {fullname}"
        ]

    print("\n".join(commands))
    subprocess.run("\n".join(commands), shell=True)
    run_command(f"docker push {fullname}")
    print("Sleeping for 5 seconds to ensure push succeeded")
    time.sleep(5)

    return f"{account}.dkr.ecr.{region}.amazonaws.com/{algorithm_name}:latest"


def main():
    # Use first line of file docstring as description if it exists.
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="Build image from scratch")
    parser.add_argument("--update", action="store_true", help="Update code in image, don't re-build")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--user", required=True, help="User name")
    parser.add_argument("--cfg-path", required=True, help="Launch config")
    args = parser.parse_args()

    setup_tmp_name =  "./setup_renamed_for_sagemaker.py"
    # print(f"Renaming ./setup.py to {setup_tmp_name}")
    # os.rename("./setup.py", setup_tmp_name)
    try:
        main_after_setup_move(args)
    except:
        # os.rename(setup_tmp_name, "./setup.py")
        raise


def main_after_setup_move(args):
    image = get_image(args.user, build_image=args.build, update_image=args.update)

    ##########
    # Create session and make sure of account and region
    ##########
    sagemaker_session = sagemaker.Session()

    # provide a pre-existing role ARN as an alternative to creating a new role
    role = "arn:aws:iam::124224456861:role/service-role/SageMaker-SageMakerAllAccess"
    role_name = role.split(["/"][-1])
    print(f"SageMaker Execution Role:{role}")
    print(f"The name of the Execution role: {role_name[-1]}")

    client = boto3.client("sts")
    account = client.get_caller_identity()["Account"]
    print(f"AWS account:{account}")

    session = boto3.session.Session()
    region = session.region_name
    print(f"AWS region:{region}")

    ##########
    # Configure the training
    ##########
    base_job_name = f"{args.user.replace('.', '-')}-{NAME}"

    checkpoint_local_path = "/opt/ml/checkpoints"

    instance_count = 2
    with open(args.cfg_path, "r") as f:
        train_args = yaml.safe_load(f)
    if "name" not in train_args:
        train_args["name"] = Path(args.cfg_path).stem
    train_args["logs"] = checkpoint_local_path if not args.local else "./logs/debug"

    def get_job_name(base, train_args):
        now = datetime.now()
        # Format example: 2023-03-03-10-14-02-324
        now_ms_str = f"{now.microsecond // 1000:03d}"
        date_str = f"{now.strftime('%Y-%m-%d-%H-%M-%S')}-{now_ms_str}"

        job_name = "_".join([base, train_args["name"], date_str])

        return job_name


    job_name = get_job_name(base_job_name, train_args)

    output_root = f"s3://tri-ml-sandbox-16011-us-east-1-datasets/sagemaker/{args.user}/{NAME}/"
    output_s3 = os.path.join(output_root, job_name)

    estimator = PyTorch(
        entry_point="open_lm/main.py",
        base_job_name=base_job_name,
        hyperparameters=train_args,
        role=role,
        image_uri=image,
        instance_count=instance_count,
        instance_type="local_gpu" if args.local else "ml.p4d.24xlarge",
        # sagemaker_session=sagemaker_session,
        output_path=output_s3,
        job_name=job_name,
        checkpoint_s3_uri=None if args.local else f"{output_s3}/checkpoint",
        checkpoint_local_path=None if args.local else checkpoint_local_path,
        code_location=output_s3,
        # Training using SMDataParallel Distributed Training Framework
        distribution={"torch_distributed": {"enabled": True}},
        # Max run 10 days
        max_run=5 * 24 * 60 * 60,
        # max_run=60 * 60,  # 60 minutes
        input_mode="FastFile",
        # environment={"TORCH_DISTRIBUTED_DEBUG": "DETAIL", "TORCH_CPP_LOG_LEVEL": "INFO"},
        keep_alive_period_in_seconds=30 * 60,  # 30 minutes
        dependencies=[SSHEstimatorWrapper.dependency_dir()],
    )

    # ssh_wrapper = SSHEstimatorWrapper.create(estimator, connection_wait_time_seconds=600)
    # dataset_location = "s3://tri-ml-datasets/scratch/achal.dave/projects/lavis/data/"
    estimator.fit(
        # inputs={"datasets": TrainingInput(dataset_location, input_mode="FastFile")}
    )
    # print("Job name:", estimator.latest_training_job.name)
    # print(f"To connect over SSH run: sm-local-ssh-training connect {ssh_wrapper.training_job_name()}")

    # instance_ids = ssh_wrapper.get_instance_ids(timeout_in_sec=900)  # <--NEW--

    # print(f"To connect over SSM run: aws ssm start-session --target {instance_ids[0]}")
    # estimator.logs()


if __name__ == "__main__":
    main()
