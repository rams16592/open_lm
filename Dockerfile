# SageMaker PyTorch image
FROM 763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:2.0.1-gpu-py310-cu118-ubuntu20.04-sagemaker

# Run custom installation of libraries
# RUN pip install xxx
# RUN apt-get update && apt-get install -y xxx
# ENV <your environment variables>
# etc....

# Remove the conda installed symlink for libcurl, which causes an error with curl.
# Fixes the following error:
# curl: /opt/conda/lib/libcurl.so.4: no version information available (required by curl)
RUN rm /opt/conda/lib/libcurl.so.4

ENV PATH="/opt/ml/code:${PATH}"

# this environment variable is used by the SageMaker PyTorch container to determine our user code directory.
ENV SAGEMAKER_SUBMIT_DIRECTORY /opt/ml/code

# /opt/ml and all subdirectories are utilized by SageMaker, use the /code subdirectory to store your user code.
COPY . /opt/ml/code/

RUN pip install -r /opt/ml/code/requirements.txt
RUN pip install -e /opt/ml/code/
# # Prevent sagemaker from installing requirements again.
# RUN rm /opt/ml/code/setup.py
RUN rm /opt/ml/code/requirements.txt

# Defines a script entrypoint 
ENV SAGEMAKER_PROGRAM open_lm/main.py

