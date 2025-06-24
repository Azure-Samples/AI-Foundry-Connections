# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------

"""
DESCRIPTION:
    This sample demonstrates how to use the Databricks connector in 
    Azure AI Foundry with Databricks to access Genie (Using Databricks AI Bridge).

USAGE:
    python sample_agent_adb_genie.py

    Before running the sample:

    pip install azure-ai-projects azure-ai-agents azure-identity databricks-ai-bridge databricks-sdk

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
from databricks_ai_bridge.genie import Genie, GenieResponse
from azure.ai.agents.models import (FunctionTool, ToolSet)
from typing import Any, Callable, Set
import os

os.environ["DATABRICKS_SDK_UPSTREAM"] = "AzureAIFoundry"
os.environ["DATABRICKS_SDK_UPSTREAM_VERSION"] = "1.0.0"

DATABRICKS_ENTRA_ID_AUDIENCE_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default" 
# Well known Entra ID audience for Azure Databricks - https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/user-aad-token

FOUNDRY_PROJECT_ENDPOINT = "<FOUNDRY_PROJECT_ENDPOINT>"
FOUNDRY_DATABRICKS_CONNECTION_NAME = "<FOUNDRY_DATABRICKS_CONNECTION_NAME>"

GENIE_QUESTION = "Describe my dataset"

##################
# Utility functions

def genie_to_object(genie_response: GenieResponse) -> dict:
    query = genie_response.query
    result = genie_response.result
    description = genie_response.description
    return {"query": query, "result": result, "description": description}

def ask_genie(questions) -> str:
    """
    Function to ask Genie a question and get the response.
    :param questions: List of questions to ask Genie.
    :return: Response from Genie.
    """
    genie_response = genie.ask_question(questions)
    return json.dumps(genie_to_object(genie_response))

##################


credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)


project_client = AIProjectClient(
    FOUNDRY_PROJECT_ENDPOINT,
    credential
)
print(f"AI Project client created for project endpoint: {FOUNDRY_PROJECT_ENDPOINT}")


connection = project_client.connections.get(FOUNDRY_DATABRICKS_CONNECTION_NAME)
print(f"Retrieved connection '{FOUNDRY_DATABRICKS_CONNECTION_NAME}' from AI project")

if connection.metadata['azure_databricks_connection_type'] == 'genie':
    genie_space_id = connection.metadata['genie_space_id']
    print(f"Connection is of type 'genie', retrieved genie space ID: {genie_space_id}")
else:
    raise ValueError("Connection is not of type 'genie', please check the connection type.")


databricks_workspace_client = WorkspaceClient(
    host=connection.target,
    token = credential.get_token(DATABRICKS_ENTRA_ID_AUDIENCE_SCOPE).token,
)
print(f"Databricks workspace client created for host: {connection.target}")


genie = Genie(genie_space_id, client=databricks_workspace_client)
print("Genie client initialized")


toolset = ToolSet()
user_functions: Set[Callable[..., Any]] = { ask_genie }
functions = FunctionTool(functions=user_functions)
toolset.add(functions)


with project_client:
    # Create an agent and run user's request with ask_genie function
    project_client.agents.enable_auto_function_calls(toolset)

    agent = project_client.agents.create_agent(
        model='<MODEL_DEPLOYMENT_NAME>',
        name="Databricks Agent",
        instructions="You're an helpful assistant, use the Databricks Genie to answer questions.",
        toolset=toolset,
    )

    print(f"Agent '{agent.name}' created with model '{agent.model}'")

    thread = project_client.agents.threads.create()
    print(f"Created thread, ID: {thread.id}")

    message = project_client.agents.messages.create(
        thread_id=thread.id,
        role="user",
        content=GENIE_QUESTION,
    )
    print(f"Created message, ID: {message.id}")

    print("Creating and processing run")

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