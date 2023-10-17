FROM 763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:2.0.1-gpu-py310-cu121-ubuntu20.04-ec2

# Remove the conda installed symlink for libcurl, which causes an error with curl.
# Fixes the following error:
# curl: /opt/conda/lib/libcurl.so.4: no version information available (required by curl)
RUN rm /opt/conda/lib/libcurl.so.4

ARG PYTORCH_VERSION=2.0.1
ARG PYTHON_SHORT_VERSION=3.10
ARG EFA_VERSION=1.14.1
ARG SMDATAPARALLEL_BINARY=https://smdataparallel.s3.amazonaws.com/binary/pytorch/2.0.1/cu121/2023-09-14/smdistributed_dataparallel-1.8.1-cp310-cp310-linux_x86_64.whl
ARG PT_S3_WHL_GPU=https://aws-s3-plugin.s3.us-west-2.amazonaws.com/binaries/0.0.1/1c3e69e/awsio-0.0.1-cp38-cp38-manylinux1_x86_64.whl
ARG CONDA_PREFIX="/opt/conda"
ARG BRANCH_OFI=1.1.3-aws

# Set ENV variables required to build PyTorch
ENV TORCH_CUDA_ARCH_LIST="5.2;7.0+PTX;7.5;8.0;8.6;9.0"
ENV TORCH_NVCC_FLAGS="-Xfatbin -compress-all"
ENV NCCL_VERSION=2.10.3

# Add OpenMPI to the path.
ENV PATH /opt/amazon/openmpi/bin:$PATH

# Add Conda to path
ENV PATH $CONDA_PREFIX/bin:$PATH

# Set this enviroment variable for SageMaker to launch SMDDP correctly.
ENV SAGEMAKER_TRAINING_MODULE=sagemaker_pytorch_container.training:main

# Add enviroment variable for processes to be able to call fork()
ENV RDMAV_FORK_SAFE=1

# Indicate the container type
ENV DLC_CONTAINER_TYPE=training

# Add EFA and SMDDP to LD library path
ENV LD_LIBRARY_PATH="/opt/conda/lib/python${PYTHON_SHORT_VERSION}/site-packages/smdistributed/dataparallel/lib:$LD_LIBRARY_PATH"
ENV LD_LIBRARY_PATH=/opt/amazon/efa/lib/:$LD_LIBRARY_PATH

RUN --mount=type=cache,id=apt-final,target=/var/cache/apt \
    apt-get update && apt-get install -y  --no-install-recommends \
        curl \
        wget \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN DEBIAN_FRONTEND=noninteractive apt-get update
# RUN mkdir /tmp/efa \
#     && cd /tmp/efa \
#     && curl --silent -O https://efa-installer.amazonaws.com/aws-efa-installer-${EFA_VERSION}.tar.gz \
#     && tar -xf aws-efa-installer-${EFA_VERSION}.tar.gz \
#     && cd aws-efa-installer \
#     && ./efa_installer.sh -y --skip-kmod -g \
#     && rm -rf /tmp/efa

# RUN curl -fsSL -v -o ~/miniconda.sh -O  https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh  && \
#     chmod +x ~/miniconda.sh && \
#     ~/miniconda.sh -b -p $CONDA_PREFIX && \
#     rm ~/miniconda.sh && \
#     $CONDA_PREFIX/bin/conda install -y python=${PYTHON_SHORT_VERSION} conda-build pyyaml numpy ipython && \
#     $CONDA_PREFIX/bin/conda clean -ya

RUN pip install sagemaker-training
RUN pip install --no-cache-dir -U \
    smclarify \
    "sagemaker>=2,<3" \
    sagemaker-experiments==0.* \
    sagemaker-pytorch-training

# Run custom installation of libraries
# RUN pip install xxx
# RUN apt-get update && apt-get install -y xxx
# ENV <your environment variables>
# etc....

ENV PATH="/opt/ml/code:${PATH}"

# this environment variable is used by the SageMaker PyTorch container to determine our user code directory.
ENV SAGEMAKER_SUBMIT_DIRECTORY /opt/ml/code

# /opt/ml and all subdirectories are utilized by SageMaker, use the /code subdirectory to store your user code.
COPY . /opt/ml/code/

RUN pip install -r /opt/ml/code/requirements.txt
# # Prevent sagemaker from installing requirements again.
# RUN rm /opt/ml/code/setup.py
RUN rm /opt/ml/code/requirements.txt

# Defines a script entrypoint 
ENV SAGEMAKER_PROGRAM open_lm/main.py

