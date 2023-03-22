import boto3
import sagemaker.session
import json
import os
import argparse
from sagemaker.processing import ScriptProcessor, FrameworkProcessor
from sagemaker.workflow.steps import ProcessingStep, TrainingStep
from sagemaker.processing import ProcessingInput, ProcessingOutput
from sagemaker.workflow.properties import PropertyFile
from sagemaker.workflow.parameters import ParameterInteger
from sagemaker.model_metrics import MetricsSource, ModelMetrics
from sagemaker.workflow.conditions import ConditionGreaterThanOrEqualTo
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.workflow.functions import JsonGet
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.step_collections import RegisterModel
from sagemaker.estimator import Estimator
from sagemaker.inputs import TrainingInput
from sagemaker.model import Model
from sagemaker.sklearn.model import SKLearnModel
from sagemaker import PipelineModel
from sagemaker.workflow.steps import CacheConfig
from sagemaker.sklearn.processing import SKLearnProcessor
from sagemaker.huggingface import HuggingFaceProcessor, HuggingFace
from sagemaker.workflow.pipeline_experiment_config import PipelineExperimentConfig
from sagemaker.workflow.execution_variables import ExecutionVariables
from sagemaker.workflow.functions import Join
from sagemaker.workflow.pipeline_context import PipelineSession


parser = argparse.ArgumentParser()
parser.add_argument('--account', type=str, default="101436505502")
parser.add_argument('--region', type=str, default="eu-west-3")
parser.add_argument('--pipeline_name', type=str, default="training-pipeline")
args = parser.parse_args()

region = boto3.Session(region_name=args.region).region_name

sagemaker_session = PipelineSession()#sagemaker.session.Session()

# try:
#     role = sagemaker.get_execution_role()
# except ValueError:

iam = boto3.client("iam")
role = iam.get_role(RoleName=f"{args.account}-sagemaker-exec")['Role']['Arn']

print(role)
default_bucket = sagemaker_session.default_bucket()
image_uri = f"{args.account}.dkr.ecr.{args.region}.amazonaws.com/{args.pipeline_name}:latest"

model_path = f"s3://{default_bucket}/model"
data_path = f"s3://{default_bucket}/data"
model_package_group_name = f"{args.pipeline_name}ModelGroup"
pipeline_name = args.pipeline_name

gpu_instance_type = "ml.g4dn.xlarge"
pytorch_version = "1.6.0"  # "1.6"
transformers_version = "4.11.0"  # "4.4"

# ------------ Pipeline Parameters ------------

epoch_count = ParameterInteger(
    name="epochs",
    default_value=1
)
batch_size = ParameterInteger(
    name="batch_size",
    default_value=10
)

# ------------ Preprocess ------------

script_preprocess = FrameworkProcessor(
    instance_type="ml.t3.medium",
    image_uri=image_uri,
    instance_count=1,
    base_job_name="preprocess-script",
    role=role,
    sagemaker_session=sagemaker_session,
    command=["python3"],
    estimator_cls=sagemaker.sklearn.estimator.SKLearn,
    framework_version="0.20.0",
)

preprocess_step_args = script_preprocess.run(
     inputs=[
        ProcessingInput(
            source=os.path.join(data_path, "train.csv"),
            destination="/opt/ml/processing/input/train",
        ),
        ProcessingInput(
            source=os.path.join(data_path, "test.csv"),
            destination="/opt/ml/processing/input/test",
        ),
    ],
    outputs=[
        ProcessingOutput(output_name="train",
                         source="/opt/ml/processing/output/train"),
        ProcessingOutput(output_name="test",
                         source="/opt/ml/processing/output/test"),
        ProcessingOutput(output_name="labels",
                         source="/opt/ml/processing/output/labels"),

    ],
    code="preprocess.py",
    source_dir="src",
)

step_preprocess = ProcessingStep(
    name="preprocess-data",
    step_args=preprocess_step_args,
    cache_config=CacheConfig(enable_caching=True, expire_after="30d")
)

# ------------ Train ------------

estimator = Estimator(
    image_uri=image_uri,
    instance_type=gpu_instance_type,
    instance_count=1,
    source_dir="src",
    entry_point="train.py",
    sagemaker_session=sagemaker_session,
    role=role,
    output_path=model_path,
)

estimator.set_hyperparameters(
    epoch_count=epoch_count,
    batch_size=batch_size,

)

step_train = TrainingStep(
    name="train-model",
    estimator=estimator,
    description="train-model1",
    display_name="train-model2",
    inputs={
        "train": TrainingInput(
            s3_data=step_preprocess.properties.ProcessingOutputConfig.Outputs[
                "train"].S3Output.S3Uri,
            content_type="text/csv",
        ),
        "labels": TrainingInput(
            s3_data=step_preprocess.properties.ProcessingOutputConfig.Outputs[
                "labels"].S3Output.S3Uri,
            content_type="text/csv",
        )
    },
)

# ------------ Eval ------------

# script_eval = ScriptProcessor(
#     image_uri=training_image_uri,
#     command=["python3"],
#     instance_type=huggingface_instance_type,
#     instance_count=1,
#     base_job_name="script-eval",
#     role=role,
# )

script_eval = FrameworkProcessor(
    instance_type=gpu_instance_type,
    image_uri=image_uri,
    instance_count=1,
    base_job_name="eval-script",
    role=role,
    sagemaker_session=sagemaker_session,
    command=["python3"],
    # following args need to be provided, but don't have an effect
    # because custome image is used
    estimator_cls=sagemaker.sklearn.estimator.SKLearn,
    framework_version="0.20.0",
)

evaluation_report = PropertyFile(
    name="EvaluationReport",
    output_name="evaluation",
    path="evaluation.json"
)

eval_step_args = script_eval.run(
    inputs=[
        ProcessingInput(
            source=step_preprocess.properties.ProcessingOutputConfig.Outputs[
                "test"
            ].S3Output.S3Uri,
            destination="/opt/ml/processing/test",
        ),
        ProcessingInput(
            source=step_train.properties.ModelArtifacts.S3ModelArtifacts,
            destination="/opt/ml/processing/model",
        ),
        ProcessingInput(
            source=step_preprocess.properties.ProcessingOutputConfig.Outputs[
                "labels"].S3Output.S3Uri,
            destination="/opt/ml/processing/labels",
        ),
    ],
    outputs=[
        ProcessingOutput(output_name="evaluation",
                         source="/opt/ml/processing/evaluation"),
    ],
    code="eval.py",
    source_dir="src",
)

step_eval = ProcessingStep(
    name="eval-model",
    step_args=eval_step_args,
    property_files=[evaluation_report],
)

# ------------ Register ------------

evaluation_s3_uri = "{}/evaluation.json".format(
    step_eval.arguments["ProcessingOutputConfig"]["Outputs"][0]["S3Output"]["S3Uri"]
)

model_metrics = ModelMetrics(
    model_statistics=MetricsSource(
        s3_uri=evaluation_s3_uri,
        content_type="application/json",
    )
)


# scaler_model = SKLearnModel(
#     model_data=scaler_model_data,
#     role=role,
#     sagemaker_session=sagemaker_session,
#     entry_point="inference/preprocess.py",
#     # framework_version=sklearn_version,
#     name="preprocess_model",
# )


model = Model(
    name="custom_model",
    image_uri=image_uri,
    model_data=step_train.properties.ModelArtifacts.S3ModelArtifacts,
    sagemaker_session=sagemaker_session,
    entry_point="src/inference/model.py",
    role=role,
)

# combine preprocessor and model into one pipeline-model
pipeline_model = PipelineModel(
    models=[model], role=role, sagemaker_session=sagemaker_session
)

step_register = RegisterModel(
    name="register-model",
    model=pipeline_model,
    content_types=["text/csv"],
    response_types=["text/csv"],
    inference_instances=["ml.t2.medium", "ml.m5.large"],
    transform_instances=["ml.m5.large"],
    model_package_group_name=model_package_group_name,
    model_metrics=model_metrics,
)

# ------------ Deploy (not used in pipeline) ------------

script_deploy = ScriptProcessor(
    image_uri=image_uri,
    command=["python3"],
    instance_type="ml.t3.medium",
    instance_count=1,
    base_job_name="script-workshop-deploy",
    role=role,
)

step_deploy = ProcessingStep(
    name="workshop-deploy-model",
    processor=script_deploy,
    inputs=[],
    outputs=[],
    code="src/deploy.py",
    property_files=[],
)


# ------------ Condition ------------

cond_gte = ConditionGreaterThanOrEqualTo(
    left=JsonGet(
        step_name=step_eval.name,
        property_file=evaluation_report,
        json_path="metrics.accuracy.value"
    ),
    right=0.1
)

step_cond = ConditionStep(
    name="accuracy-check",
    conditions=[cond_gte],
    if_steps=[step_register],
    else_steps=[],
)

#  ------------ build Pipeline ------------

pipeline = Pipeline(
    name=pipeline_name,
    parameters=[
        epoch_count,
        batch_size
    ],
    steps=[
        step_preprocess,
        step_train,
        step_eval,
        step_cond,
    ],
    sagemaker_session=sagemaker_session,
    pipeline_experiment_config=None,
)


if __name__ == '__main__':
    json.loads(pipeline.definition())
    pipeline.upsert(role_arn=role)
    execution = pipeline.start()
    execution = execution.wait()
