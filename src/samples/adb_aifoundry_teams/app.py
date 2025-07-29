# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.


import re, os
from os import environ, path
import sys, requests, traceback
import json
from pathlib import Path
from typing import Any, Callable, Set, Dict, List, Optional
from aiohttp.web import Application, Request, Response, run_app
from dotenv import load_dotenv

from microsoft.agents.hosting.aiohttp import (
    CloudAdapter,
    jwt_authorization_middleware,
    start_agent_process,
)
from microsoft.agents.hosting.core import (
    Authorization,
    AgentApplication,
    TurnState,
    TurnContext,
    MemoryStorage,
    MessageFactory
)

from microsoft.agents.activity import load_configuration_from_env, ActivityTypes, Attachment

from microsoft.agents.authentication.msal import MsalConnectionManager

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from azure.ai.agents.models import (
    AsyncToolSet,
    AsyncFunctionTool, 
    RequiredFunctionToolCall,
    SubmitToolOutputsAction,
    ToolOutput,
    CodeInterpreterTool,
)

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import GenieAPI

from azure.storage.blob import BlobServiceClient, ContentSettings

from start_server import start_server

class TeamsAppCustomException(Exception):
    def __init__(self, message):
        super().__init__(message)

#Global variables
adbtoken = None
genie_spaceid = None
project_client = None
agent = None
thread = None
invalid_foundry_connection = False

#Load environment variables from .env file
load_dotenv(path.join(path.dirname(__file__), ".env"))

agents_sdk_config = load_configuration_from_env(environ)

STORAGE = MemoryStorage()
CONNECTION_MANAGER = MsalConnectionManager(**agents_sdk_config)
ADAPTER = CloudAdapter(connection_manager=CONNECTION_MANAGER)
AUTHORIZATION = Authorization(STORAGE, CONNECTION_MANAGER, **agents_sdk_config)

#Configure Agent_App
AGENT_APP = AgentApplication[TurnState](
    storage=STORAGE, adapter=ADAPTER, authorization=AUTHORIZATION, **agents_sdk_config
)

#Get values of environment variables
FOUNDRY_URL= os.getenv("FOUNDRY_URL","")
ADB_CONNECTION_NAME=os.getenv("ADB_CONNECTION_NAME","")

STORAGE_ACCTNAME=os.getenv("STORAGE_ACCTNAME","")
STORAGE_CONTNAME=os.getenv("STORAGE_CONTNAME","")

#Create images directory if it doesn't exist   
IMAGES_DIR = os.path.join(os.getcwd(), "images")
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR)
    IMAGES_DIR = os.path.join(os.getcwd(), "images")

#Function to retrieive the Azure Databricks Token via the OBO (On bhalf of) flow
async def getadbtoken(passed_user_access_token):
    """
    Function to retrieive the following
    - Use the Foundry URL and ADB Connection configured on AI Foundry portal to dynamically fetch the Genie Space ID.
    - Azure Databricks Token via the OBO (On bhalf of) flow 
    """

    global project_client, genie_spaceid, adbtoken, invalid_foundry_connection

    try:

        ADB_CONNECTION_NAME = os.getenv("ADB_CONNECTION_NAME","")

        cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)

        project_client = AIProjectClient(FOUNDRY_URL, cred)

        #Dynamically fetch the Azure Databricks Genie Space ID
        connection = project_client.connections.get(ADB_CONNECTION_NAME)
        if connection.metadata['azure_databricks_connection_type'] == 'genie':
            genie_spaceid = connection.metadata['genie_space_id']
        else:
            genie_spaceid = None

        if (genie_spaceid == None): invalid_foundry_connection = True

        if (genie_spaceid != None):  
            print(f"Azure Databricks Genie Space ID: {genie_spaceid}")

            #Get the ABD token via the OBO flow
            host      = 'https://login.microsoftonline.com/'+os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID")+'/'
            client_id = os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID")
            oauth_secret=os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET")
            endpoint='oauth2/v2.0/token'
            url = f'{host}{endpoint}'
            DATABRICKS_RESOURCE  = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"

            data = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "scope": f"{DATABRICKS_RESOURCE}/.default",
                "requested_token_use": "on_behalf_of",
                "assertion": passed_user_access_token,
            }

            auth = (client_id,oauth_secret)
            oauth_response = requests.post(url, data=data,auth=auth)
            #print(oauth_response)

            #We got the ADB token that will be used by Genie API
            adbtoken = oauth_response.json()['access_token']
            return adbtoken

    except Exception as e:
        raise TeamsAppCustomException("Error obtaining Azure Databricks Connection from Azure Foundry Project or unable to get ADB token via OBO Flow! ")

#Function to retrieive the Graph Token based on the user's ID token
async def getgraphtoken(passed_user_access_token):

    """
    Function to grab the appropriate token to make graph api calls. 
    """

    try:
        
        host      = 'https://login.microsoftonline.com/'+os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID")+'/'
        client_id = os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID")
        oauth_secret=os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET")
        endpoint='oauth2/v2.0/token'
        url = f'{host}{endpoint}'

        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "scope": "https://graph.microsoft.com/.default",
            "requested_token_use": "on_behalf_of",
            "assertion": passed_user_access_token,
        }

        auth = (client_id,oauth_secret)
        oauth_response = requests.post(url, data=data,auth=auth)
        #print(oauth_response)

        oauth_access_token = oauth_response.json()['access_token']
        return oauth_access_token
    except Exception as e:
        raise TeamsAppCustomException("Error obtaining the user's profile via graph api! ")

@AGENT_APP.activity(ActivityTypes.invoke)
async def invoke(context: TurnContext, state: TurnState) -> str:
    """
    Internal method to process template expansion or function invocation.
    """
    await AGENT_APP.auth.begin_or_continue_flow(context, state)

@AGENT_APP.on_sign_in_success
async def handle_sign_in_success(
    context: TurnContext, state: TurnState, handler_id: str = None
) -> bool:
    """
    Internal method to handle successful sign-in events.
    """
    await context.send_activity(
        MessageFactory.text(
            f"Successfully signed in to {handler_id or 'service'}. You can now use authorized features."
        )
    )

@AGENT_APP.conversation_update("membersAdded")
async def on_members_added(context: TurnContext, _state: TurnState):
    await context.send_activity(
        "Welcome to the ADB-AIFoundry-Teams demo!"
        "For OAuth flows, enter the 6-digit verification code when prompted."
    )
    return True

@AGENT_APP.error
async def on_error(context: TurnContext, error: Exception):
    # This check writes out errors to console log .vs. app insights.
    # NOTE: In production environment, you should consider logging this to Azure
    #       application insights.
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    # Send a message to the user
    await context.send_activity("The bot encountered an error or bug.")

#Decortator to ensure that the user's ID token is retrieved dynamically by using handler. 
#Handler name needs to match what's listed in the .env variable name 
@AGENT_APP.message(re.compile(r".*", re.IGNORECASE),auth_handlers=["GRAPH"])
async def on_message(context: TurnContext, state: TurnState):
    
    global adbtoken

    prompt = context.activity.text.strip()

    await context.send_activity("Agent is thinking...Thanks for being patient...")

    try:
        if adbtoken == None or adbtoken == "":
            user_access_token = await AGENT_APP.auth.get_token(context, "GRAPH")
            adbtoken = await getadbtoken(user_access_token.token)
            #await context.send_activity(f"Your ADB token: {adbtoken}")
    except TeamsAppCustomException as e:
            await context.send_activity(MessageFactory.text("Error occurred while fetching ADB token."))
            return
        
    response = ""
    imageurl = None

    try:
        if (genie_spaceid == None) or (adbtoken == None):
            if (invalid_foundry_connection == True):
                await context.send_activity(MessageFactory.text("Azure Foundry URL is either incorrect or the Databaricks Genie connection isn't configured for the Azure AI Foundry project ."))
                return
        response, imageurl = await processmessage(prompt)
        #Send the response from Genie api to the user
        if (response != None or response != ""): await context.send_activity(MessageFactory.text(response))
        #send adaptive card to the user via Teams and then delete the blob file from the Azure Blob container 
        if (imageurl != None): 
            await _send_custom_card(context, imageurl)
            #await self.del_blob_file(imageurl)

    except Exception as e:
        await context.send_activity(MessageFactory.text(traceback.format_exc()))

#Call Genie APIs by sending the user's prompts/questions
async def ask_genie(question: str, conversation_id: str = None) -> tuple[str, str]:
        """
        Ask Genie a question and return the response as JSON.
        param question: The question to ask Genie.
        param conversation_id: The ID of the conversation to continue. If None, a new conversation will be started.
        """

        global global_conversation_id

        DATABRICKS_HOST = os.getenv("DATABRICKS_HOST","")
        #DATABRICKS_TOKEN = os.getenv("DATABRICKS_TOKEN")
        #genie_spaceid = os.getenv("DATABRICKS_SPACE_ID")
        
                                             
        #Comment the next line and try an alternative
        #workspace_client = WorkspaceClient(host=DATABRICKS_HOST,token=DATABRICKS_TOKEN)
        workspace_client = WorkspaceClient(host=DATABRICKS_HOST,token=adbtoken)

        #We should have the the genie_spaceid by now

        genie_api = GenieAPI(workspace_client.api_client)


        try:
            if conversation_id is None:
                message = genie_api.start_conversation_and_wait(genie_spaceid, question)
                conversation_id = message.conversation_id
            else:
                message = genie_api.create_message_and_wait(genie_spaceid, conversation_id, question)

            query_result = None
            if message.query_result:
                query_result = genie_api.get_message_query_result(
                    genie_spaceid, message.conversation_id, message.id
                )

            message_content = genie_api.get_message(genie_spaceid, message.conversation_id, message.id)

            # Try to parse structured data if available
            if query_result and query_result.statement_response:
                statement_id = query_result.statement_response.statement_id
                results = workspace_client.statement_execution.get_statement(statement_id)

                columns = results.manifest.schema.columns
                data = results.result.data_array

                # Format as markdown table
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

#Ensure that Genie APIs are callable by the agent dynamically
genie_funcs: Set[Callable[..., Any]] = {
    ask_genie,
}

#Function called by the on_message function
async def processmessage(question: str, conversation_id: str = None) -> tuple[str, str]:

    """
    Main funcion to process the user's prompt and let the agent pass the prompt to the FunctionTool attached to the agent.
    """

    #No need for the next 2 lines
    #cred = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    #project_client = AIProjectClient(FOUNDRY_URL, cred)

    #global agent, thread

    custom_functions = AsyncFunctionTool(genie_funcs)
    toolset = AsyncToolSet()
    toolset.add(custom_functions)
    toolset.add(CodeInterpreterTool())

    response = ""

    try:

        agent_client = project_client.agents
        
        #agent_client.enable_auto_function_calls(tools=toolset)

        agent = agent_client.create_agent(
            name="my-assistant", 
            model = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o"), 
            instructions = ("""
                Your are an agent that responds to user questions related to Sales and pipeline just in Americas and by segment, sales of global customers, opportunity size, revenue by customers and customer churn. 
                For all questions you must solely rely on the function ask_genie and rely on the following instructions. 
                1. Instructions to use the ask_genie function
                    - You must use the same prompt as the user question and never change the user's prompt.
                    - Use the previous conversation_id if it's available.
                    - You must use the code interpreter tool for any vizualation related questiins or prompts.
                    - You must get the tabular data from the ask_genie function and render it via the markdown format before presenting the analysis of the data. 
                    - Please use the markdown format to display tabular data before rendering any visualization via the code interpreter tool.
                2. Instructions on the visualization and code interpretation
                    - Test and display visualization code using the code interpreter, retrying if errors occur.
                    - Always use charts or graphs to illustrate trends when requested.
                    - Always create visualizations as `.png` files.
                    - Adapt visualizations (e.g., labels) to the user's language preferences.
                    - When asked to download data, default to a `.csv` format file and use the most recent data.
                    - Do not ever render the code or include file download links in the response.
                """
                ),
                toolset=toolset,
            )
                
        #except Exception as e:
        #    return "Wasn't able to create the agent or add the tool to the agent."

        thread = agent_client.threads.create()
        #print(f"Created thread, thread ID: {thread.id}")

        message = agent_client.messages.create(thread_id=thread.id, role='user', content=question)

        run = agent_client.runs.create(
            thread_id=thread.id,
            agent_id=agent.id,
        )
        print(f"Agent, thread, message, run: {agent.id}, {thread.id}, {message.id}, {run.id}")

        # Polling loop for run status
        while run.status in ["queued", "in_progress", "requires_action"]:
            run =  agent_client.runs.get(thread_id=thread.id, run_id=run.id)

            if run.status == "requires_action" and isinstance(run.required_action, SubmitToolOutputsAction):
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                if not tool_calls:
                    print("No tool calls provided - cancelling run")
                    agent_client.runs.cancel(thread_id=thread.id, run_id=run.id)
                    break

                tool_outputs = []
                for tool_call in tool_calls:
                    if isinstance(tool_call, RequiredFunctionToolCall):
                        try:
                            output = await custom_functions.execute(tool_call)
                            tool_outputs.append(
                                ToolOutput(
                                    tool_call_id=tool_call.id,
                                    output=output,
                                )
                            )
                        except Exception as e:
                            print(f"Error executing tool_call {tool_call.id}: {e}")

                print(f"Tool outputs: {tool_outputs}")
                if tool_outputs:
                    agent_client.runs.submit_tool_outputs(
                        thread_id=thread.id, run_id=run.id, tool_outputs=tool_outputs
                    )

        messages = agent_client.messages.list(thread_id=thread.id, run_id=run.id)
        print(f"Messages: {messages}")

        file_name = None

        latest_message = None
        for msg in messages:
            latest_message = msg
            break
        
        if latest_message and latest_message.content:
            for content_item in latest_message.content:
                if content_item.type == 'text':
                    response = content_item.text.value
                elif content_item.type == 'image_file':
                    has_images = True
                    file_id = content_item.image_file.file_id
                    print(f"Image File ID: {file_id}")
                    file_name = f"{file_id}_image_file.png"
                    agent_client.files.save(file_id=file_id, file_name=file_name, target_dir=IMAGES_DIR)
                    #Upload the vizualization to Azure blob storage container. 
                    #Make sure that the user running the app has Storage Blob Contributor Role on the storage account and the conatiner. 
                    #Set the lifecycle policies to delete the files on a periodic basis.
                    await upload_blob_file(file_name)

        agent_client.delete_agent(agent.id)
        #print("Deleted agent")
        #project_client.close()

        return response, file_name
    
    except Exception as e:
        raise

#Function called by the on_message function
async def upload_blob_file(imagefilename):

    """
    Function to upload visualizations to Azure Blob container. - its essential to make adaptive cards show the charts via publicly accessible urls
    ROle Storage Blob Contributor needs to be assigned to the user.
    Code needs to be added to remove the files from the webserver and Blob Containers as soon as the rendering is completed.
    Appopriate security policy for Blob Container needs to be in-place as well. 
    """
    #try:

    account_url = "https://"+STORAGE_ACCTNAME+".blob.core.windows.net"
    container_name = STORAGE_CONTNAME

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
    blob_service_client = BlobServiceClient(account_url, credential=credential)

    upload_file_path = os.path.join(IMAGES_DIR, imagefilename)

    container_client = blob_service_client.get_container_client(container=container_name)
    with open(file=upload_file_path, mode="rb") as data:
        content_settings = ContentSettings(content_type="image/jpg")
        blob_client = container_client.upload_blob(name=imagefilename, data=data, content_settings=content_settings)

    #blob_client = blob_service_client.get_blob_client(container=container_name, blob=imagefilename)
    #with open(file=upload_file_path, mode="rb") as data:
    #    content_settings = ContentSettings(content_type="image/jpg")
    #    blob_client = blob_client.upload_blob(data, content_settings=content_settings)
        

    #except Exception as e:
    #    raise TeamsAppCustomException("Error uploading vizualization file to Azure Blob Storage Container - role is missing or anonymous access disabled.")

#Not being used right now.
async def del_blob_file(imagefilename):

    """
    Function to delete the blob file
    """

    try:

        account_url = "https://"+STORAGE_ACCTNAME+".blob.core.windows.net"
        container_name = STORAGE_CONTNAME

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        blob_service_client = BlobServiceClient(account_url, credential=credential)

        container_client = blob_service_client.get_container_client(container=container_name)
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=imagefilename)
        blob_client.delete_blob()
    
    except Exception as e:
        raise TeamsAppCustomException("Error deleting file from Azure Blob Storage Container.")

#Function called by the on_message function to render graph/chart in a Teams adaptive card
async def _send_custom_card(turn_context: TurnContext, imageurl):

    
    """
    Use the adaptive card to send the vizualiations (bar chart, pic chart, etc) to the user
    It relies on the charts uploaded to Azure Blob storage containers.  
    One needs to have policy in place to clean up files from blob container on a regular basis.
    Plus code needs to be added to remove the files immediately from the local/web server and Blob Container. 
    """

    """Send a custom card"""
    try:

        container_blob_file_path="https://"+STORAGE_ACCTNAME+".blob.core.windows.net/"+STORAGE_CONTNAME+"/"+imageurl

        card_data = {
            "type": "AdaptiveCard",
            "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": [
                {
                    "type": "Image",
                    "id": "0001",
                    "url": container_blob_file_path
                }
            ]
        }
    

        attachment = Attachment(
            content_type="application/vnd.microsoft.card.adaptive",
            content=card_data,
        )

        await turn_context.send_activity(MessageFactory.attachment(attachment))
        

    except Exception as e:
        await turn_context.send_activity(f"Error sending custom adaptive card: {str(e)}")

#Make sure that start_server is listed at the bottom of this file.
start_server(
    agent_application=AGENT_APP,
    auth_configuration=CONNECTION_MANAGER.get_default_connection_configuration(),
)
