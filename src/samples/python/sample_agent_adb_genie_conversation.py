# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------

"""
DESCRIPTION:
    This sample demonstrates how to use the Databricks connector in 
    Azure AI Foundry with Databricks to access Genie (using the Genie API).
    Here we ask Genie two questions and maintain the conversation context between them.

USAGE:
    python sample_agents_functions.py

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
from databricks.sdk.service.dashboards import GenieAPI
from azure.ai.agents.models import (FunctionTool, ToolSet)
from typing import Any, Callable, Set
import json
import os

os.environ["DATABRICKS_SDK_UPSTREAM"] = "AzureAIFoundry"
os.environ["DATABRICKS_SDK_UPSTREAM_VERSION"] = "1.0.0"

DATABRICKS_ENTRA_ID_AUDIENCE_SCOPE = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default" 
# Well known Entra ID audience for Azure Databricks - https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/user-aad-token

FOUNDRY_PROJECT_ENDPOINT = "<FOUNDRY_PROJECT_ENDPOINT>"
FOUNDRY_DATABRICKS_CONNECTION_NAME = "<FOUNDRY_DATABRICKS_CONNECTION_NAME>"

GENIE_QUESTION_1 = "What is the average transaction value?"

GENIE_QUESTION_2 = "How many transactions were made that were above that value?"

##################
# Utility functions

def ask_genie(question: str, conversation_id: str = None) -> str:
    """
    Ask Genie a question and return the response as JSON.
    The response JSON will contain the conversation ID and either the message content or a table of results.
    Reuse the conversation ID in future calls to continue the conversation and maintain context.
    
    param question: The question to ask Genie.
    param conversation_id: The ID of the conversation to continue. If None, a new conversation will be started.
    """
    try:
        if conversation_id is None:
            message = genie_api.start_conversation_and_wait(genie_space_id, question)
            conversation_id = message.conversation_id
        else:
            message = genie_api.create_message_and_wait(genie_space_id, conversation_id, question)

        query_result = None
        if message.query_result:
            query_result = genie_api.get_message_query_result(
                genie_space_id, message.conversation_id, message.id
            )

        message_content = genie_api.get_message(genie_space_id, message.conversation_id, message.id)

        # Try to parse structured data if available
        if query_result and query_result.statement_response:
            statement_id = query_result.statement_response.statement_id
            results = databricks_workspace_client.statement_execution.get_statement(statement_id)
            columns = results.manifest.schema.columns
            data = results.result.data_array
            headers = [col.name for col in columns]
            rows = []
            for row in data:
                formatted_row = []
                for value, col in zip(row, columns):
                    if value is None:
                        formatted_value = "NULL"
                    elif col.type_name in ["DECIMAL", "DOUBLE", "FLOAT"]:
                        formatted_value = f"{float(value):,.2f}"
                    elif col.type_name in ["INT", "BIGINT", "LONG"]:
                        formatted_value = f"{int(value):,}"
                    else:
                        formatted_value = str(value)
                    formatted_row.append(formatted_value)
                rows.append(formatted_row)
            return json.dumps({
                "conversation_id": conversation_id,
                "table": {
                    "columns": headers,
                    "rows": rows
                }
            })

        # Fallback to plain message text
        if message_content.attachments:
            for attachment in message_content.attachments:
                if attachment.text and attachment.text.content:
                    return json.dumps({
                        "conversation_id": conversation_id,
                        "message": attachment.text.content
                    })

        return json.dumps({
            "conversation_id": conversation_id,
            "message": message_content.content or "No content returned."
        })

    except Exception as e:
        return json.dumps({
            "error": "An error occurred while talking to Genie.",
            "details": str(e)
        })

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
    token = credential.get_token("2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default").token,
)
print(f"Databricks workspace client created for host: {connection.target}")

genie_api = GenieAPI(databricks_workspace_client.api_client)

toolset = ToolSet()
user_functions: Set[Callable[..., Any]] = { ask_genie }
functions = FunctionTool(functions=user_functions)
toolset.add(functions)


with project_client:
    # Create an agent and run user's request with ask_genie function
    project_client.agents.enable_auto_function_calls(toolset)

    agent = project_client.agents.create_agent(
        model='gpt-4o',
        name="Databricks Agent",
        instructions="You're an helpful assistant, use Databricks Genie to answer questions. " \
        "Use the conversation_id returned by the first call to the ask_genie function to continue the conversation in Genie.",
        toolset=toolset,
    )

    print(f"Agent '{agent.name}' created with model '{agent.model}'")

    thread = project_client.agents.threads.create()
    print(f"Created thread, ID: {thread.id}")

    agent_message_1 = project_client.agents.messages.create(
        thread_id=thread.id,
        role="user",
        content=GENIE_QUESTION_1,
    )
    print(f"Created message 1 with question: {GENIE_QUESTION_1}, ID: {agent_message_1.id}")

    print("Creating and processing Run 1")

    run_1 = project_client.agents.runs.create_and_process(
        thread_id=thread.id,
        agent_id=agent.id  
    )

    print(f"Run 1 completed with status: {run_1.status}")

    agent_message_2 = project_client.agents.messages.create(
        thread_id=thread.id,
        role="user",
        content=GENIE_QUESTION_2,
    )
    print(f"Created message 1 with question: {GENIE_QUESTION_2}, ID: {agent_message_2.id}")

    print("Creating and processing Run 2")

    run_2 = project_client.agents.runs.create_and_process(
        thread_id=thread.id,
        agent_id=agent.id  
    )

    print(f"Run 2 completed with status: {run_2.status}")

    # Delete the agent when done
    project_client.agents.delete_agent(agent.id)
    print("Deleted agent")

    # Fetch and log all messages
    messages = project_client.agents.messages.list(thread_id=thread.id)
    for message in messages:
        print(f"Message ID: {message.id}, Role: {message.role}, Content: {message.content}")

    # Fetch and log all run steps
    print("\n\nSteps in Run 1:")
    steps = project_client.agents.run_steps.list(thread_id=thread.id, run_id=run_1.id)
    for step in steps:
        print(json.dumps(step.as_dict(), indent=2))

    print("\n\nSteps in Run 2:")
    steps = project_client.agents.run_steps.list(thread_id=thread.id, run_id=run_2.id)
    for step in steps:
        print(json.dumps(step.as_dict(), indent=2))