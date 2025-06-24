# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------

"""
DESCRIPTION:
    This sample demonstrates how to use the Databricks connector in 
    Azure AI Foundry with Databricks to access Jobs.

USAGE:
    python sample_agent_adb_job.py

    Before running the sample:

    pip install azure-ai-projects azure-ai-agents azure-identity databricks-sdk

    Set these environment variables with your own values:
    1) FOUNDRY_PROJECT_ENDPOINT - The endpoint of your Azure AI Foundry project, as found in the "Overview" tab
       in your Azure AI Foundry project.
    2) FOUNDRY_DATABRICKS_CONNECTION_NAME - The name of the Databricks connection, as found in the "Connected Resources" under "Management Center" tab
       in your Azure AI Foundry project.
    2) MODEL_DEPLOYMENT_NAME - The deployment name of the AI model, as found under the "Name" column in 
       the "Models + endpoints" tab in your Azure AI Foundry project.
"""

import json
from databricks.sdk import WorkspaceClient
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import (FunctionTool, ToolSet)
from typing import Any, Callable, Set
import os

os.environ["DATABRICKS_SDK_UPSTREAM"] = "AzureAIFoundry"
os.environ["DATABRICKS_SDK_UPSTREAM_VERSION"] = "1.0.0"

DATABRICKS_ENTRA_ID_AUDIENCE_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default"
# Well known Entra ID audience for Azure Databricks - https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/user-aad-token


FOUNDRY_PROJECT_ENDPOINT = "<FOUNDRY_PROJECT_ENDPOINT>"
FOUNDRY_DATABRICKS_CONNECTION_NAME = "<FOUNDRY_DATABRICKS_CONNECTION_NAME>"

##################
# Utility functions

def run_job_and_wait(job_id: str) -> str:
    """
    Function to run a job in Databricks.
    :param job_id: ID of the job to run.
    :return: Job run id
    """
    job_run = databricks_workspace_client.jobs.run_now_and_wait(job_id=job_id)
    return json.dumps({
        "job_run_id": job_run.run_id,
    })

def get_job_output(job_run_id: str) -> str:
    """
    Function to get the output of a job run in Databricks.
    :param job_run_id: ID of the job run to check.
    :return: Output of each task in the job run.
    """
    task_runs = databricks_workspace_client.jobs.get_run(run_id=int(job_run_id)).tasks
    job_output = {
        'task_runs': {}
    }
    for task_run in task_runs:
        task_run_id = task_run.run_id
        job_output['task_runs'][task_run_id] = databricks_workspace_client.jobs.get_run_output(run_id=task_run_id).as_dict()
    return json.dumps(job_output)

##################


credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)


project_client = AIProjectClient(
    FOUNDRY_PROJECT_ENDPOINT,
    credential
)
print(f"AI Project client created for project endpoint: {FOUNDRY_PROJECT_ENDPOINT}")


connection = project_client.connections.get(FOUNDRY_DATABRICKS_CONNECTION_NAME)
print(f"Retrieved connection '{FOUNDRY_DATABRICKS_CONNECTION_NAME}' from AI project")

if connection.metadata['azure_databricks_connection_type'] == 'job':
    job_id = connection.metadata['job_id']
    print(f"Connection is of type 'job', retrieved job ID: {job_id}")
else:
    raise ValueError("Connection is not of type 'job', please check the connection type.")

databricks_workspace_client = WorkspaceClient(
    host=connection.target,
    token = credential.get_token(DATABRICKS_ENTRA_ID_AUDIENCE_SCOPE).token,
)
print(f"Databricks workspace client created for host: {connection.target}")

toolset = ToolSet()
user_functions: Set[Callable[..., Any]] = { run_job_and_wait, get_job_output }
functions = FunctionTool(functions=user_functions)
toolset.add(functions)

with project_client:
    # Create an agent and run user's request with ask_genie function
    project_client.agents.enable_auto_function_calls(toolset)

    agent = project_client.agents.create_agent(
        model='<MODEL_DEPLOYMENT_NAME>',
        name="Databricks Agent",
        instructions="You're an helpful assistant, use the Databricks Jobs API to answer questions.",
        toolset=toolset,
    )

    print(f"Agent '{agent.name}' created with model '{agent.model}'")

    thread = project_client.agents.threads.create()
    print(f"Created thread, ID: {thread.id}")

    message = project_client.agents.messages.create(
        thread_id=thread.id,
        role="user",
        content=f"Run the job with ID {job_id} and get the final output",
    )
    print(f"Created message, ID: {message.id}")

    print("Creating and processing run (Please wait this may take a while)...")

    run = project_client.agents.runs.create_and_process(
        thread_id=thread.id,
        agent_id=agent.id  
    )

    print(f"Run completed with status: {run.status}")

    # Delete the agent when done
    project_client.agents.delete_agent(agent.id)
    print("Deleted agent")

    # Fetch and log all messages
    messages = project_client.agents.messages.list(thread_id=thread.id)
    for message in messages:
        print(f"Message ID: {message.id}, Role: {message.role}, Content: {message.content}")