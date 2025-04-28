import logging
import os
import httpx
import uuid
import math

from dotenv import load_dotenv

from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import (
    openai,
    cartesia,
    deepgram,
    elevenlabs,
    # noise_cancellation,
    silero,
    groq,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit.agents import metrics, MetricsCollectedEvent
import logging

from datetime import datetime, timedelta
import asyncio
from typing import Union, Annotated, Any, Dict, List, get_type_hints, get_origin, get_args
from dataclasses import dataclass

logger = logging.getLogger("my-worker")
logger.setLevel(logging.INFO)

load_dotenv()

backend_url = os.getenv("BACKEND_URL")

silence_detection_threshold = int(os.getenv("SILENCE_DETECTION_THRESHOLD"))
print(f"silence_detection_threshold: {silence_detection_threshold}")


# Helper function to create a dynamic tool function
def create_dynamic_tool_function(tool_config: Dict[str, Any], user_id: str):
    """Creates a dynamic async function for a tool based on its configuration."""
    tool_name = tool_config.get("tool_name", "unknown_tool")
    tool_description = tool_config.get("tool_description", "")
    is_async = tool_config.get("istool_async", True)
    parameters = tool_config.get("parameters", [])
    server_settings = tool_config.get("server_settings", {})
    http_headers = tool_config.get("httpHeaders", [])
    req_type = tool_config.get("req_type", "POST").upper()  # Default to POST if not specified
    
    # Generate parameter annotations for the function
    param_annotations = {}
    param_names = []
    
    for param in parameters:
        arg_name = param.get("arg_name", "")
        arg_type = param.get("arg_type", "string")
        arg_description = param.get("arg_description", "")
        
        if not arg_name:
            continue
            
        param_names.append(arg_name)
        
        # Convert string type names to actual Python types
        python_type = str  # Default to string
        if arg_type.lower() == "integer" or arg_type.lower() == "int":
            python_type = int
        elif arg_type.lower() == "boolean" or arg_type.lower() == "bool":
            python_type = bool
        elif arg_type.lower() == "array" or arg_type.lower() == "list":
            python_type = list
        elif arg_type.lower() == "object" or arg_type.lower() == "dict":
            python_type = dict
        elif arg_type.lower() == "float" or arg_type.lower() == "number":
            python_type = float
            
        # Create Annotated type with description
        param_annotations[arg_name] = Annotated[
            python_type, llm.TypeInfo(description=arg_description)
        ]
    
    # We need to dynamically create a function with explicit parameters
    # First, create the function code as a string
    func_params = ", ".join([f"{name}: param_annotations['{name}']" for name in param_names])
    
    # Pre-generate the headers section as a string
    headers_code = ""
    for header in http_headers:
        header_name = header.get("header_name", "")
        header_value = header.get("header_value", "")
        if header_name and header_value:
            headers_code += f"headers[\"{header_name}\"] = \"{header_value}\"\n"
    
    # Create the complete function code with proper indentation
    func_code = f"""async def {tool_name}(self, {func_params}):
    \"""{tool_description}\"""
    logger.info(f"Calling dynamic tool: {tool_name}")
    
    # Extract parameters
    call_params = {{{', '.join([f"'{name}': {name}" for name in param_names])}}}
    call_params["user_id"] = "{user_id}"
    
    print("call_params:", call_params)
    
    # Get server settings
    server_url = "{server_settings.get('server_url', '')}"
    server_token = "{server_settings.get('server_token', '')}" 
    timeout_seconds = {server_settings.get('timeout_seconds', 30)}
    
    # Request type
    req_type = "{req_type}"
    
    # Prepare headers
    headers = {{}}
    {headers_code}
    if server_token and server_token != " ":
        headers["Authorization"] = f"Bearer {{server_token}}"
    
    try:
        # Make the HTTP request to the tool's endpoint based on req_type
        async with httpx.AsyncClient(timeout=float(timeout_seconds)) as client:
            # Choose the appropriate HTTP method based on req_type
            if req_type == "GET":
                # For GET requests, parameters are sent as query parameters
                response = await client.get(
                    server_url,
                    params=call_params,
                    headers=headers
                )
            else:
                # For all other request types (POST by default), parameters are sent as JSON in the body
                response = await client.post(
                    server_url,
                    json=call_params,
                    # headers=headers
                )
                
            response.raise_for_status()
            
            # Return the response as tool output
            try:
                result = response.json()
                return result
            except ValueError:
                # If the response is not JSON, return the text
                return response.text
                
    except Exception as e:
        logger.error(f"Error calling tool {tool_name}: {{str(e)}}")
        return f"Failed to call tool {tool_name}: {{str(e)}}"
"""
    
    # Create a local namespace to execute the function definition
    local_namespace = {
        'Annotated': Annotated,
        'httpx': httpx,
        'logger': logger,
        'param_annotations': param_annotations
    }
    
    # Execute the function code in the local namespace
    exec(func_code, globals(), local_namespace)
    
    # Get the created function from the local namespace
    dynamic_func = local_namespace[tool_name]
    
    # Set function annotations 
    dynamic_func.__annotations__ = {'self': None, 'return': Any, **param_annotations}
    
    return dynamic_func


async def entrypoint(ctx: agents.JobContext):
    
    
    httpclient = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))

    # Define participant event handlers *before* potentially missing the event
    call_start_time = ""
    def on_participant_connected(participant):
        logger.info(f"wohooo participant {participant.identity} connected at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        nonlocal call_start_time
        call_start_time = datetime.now()
        logger.info(f"participant connected at: {call_start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    def on_participant_left(participant):
        nonlocal call_end_time
        call_end_time = datetime.now()
        logger.info(f"participant {participant.identity} left at: {call_end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    # Register event handlers *immediately* after connecting
    ctx.room.on("participant_connected", on_participant_connected)
    ctx.room.on("participant_disconnected", on_participant_left)
    
    #get the agent_id from the room name
    room_name = ctx.room.name
    idArray = room_name.split("_")
    agent_id = idArray[0]
    call_id = idArray[1]
    user_id = idArray[2]
    
    user_joined = False # Initialize the flag

    logger.info(f"agent_id: {agent_id}")
    logger.info(f"call_id: {call_id}")
    logger.info(f"user_id: {user_id}")
    
    #send acknowledgement to the backend
    try:
        response = await httpclient.get(
            f"{backend_url}/userRecord/acknowledge/{user_id}",
        )
        response.raise_for_status()
        logger.info(f"Acknowledgement sent to the backend: {response.json()}")
    except Exception as e:
        logger.error(f"Failed to send acknowledgement: {str(e)}")

    #get the agent config details from the datastore by using the agent_id
    try:
        # Create a single client for all requests in this function            
        if not backend_url:
            logger.error("BACKEND_URL not found in environment variables")
            return
        
        # Add retry logic for production reliability
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"agent fetching attempt {attempt}")
                response = await httpclient.get(
                    f"{backend_url}/agentConfig/agentid/{agent_id}",
                    headers={"X-Request-ID": f"{agent_id}-{uuid.uuid4()}"}  # Add request tracking
                )
                response.raise_for_status()  # Ensure we got a valid response
                
                # Parse the response into a usable format
                agent_config = response.json()
                # print(f"agent_config: {agent_config}")
                # print(f"agent_config: {agent_config['system_prompt']}")
                logger.info(f"Successfully retrieved agent config for {agent_id}")
                
                break  # Exit retry loop on success
                
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error fetching agent config: {e.response.status_code} - {e.response.text}")
                if attempt == max_retries - 1:  # Last attempt
                    raise  # Re-raise on final attempt to handle at higher level
                await asyncio.sleep(min(2 ** attempt, 10))  # True exponential backoff with 10s cap
                
            except httpx.RequestError as e:
                logger.error(f"Network error fetching agent config: {str(e)}")
                if attempt == max_retries - 1:  # Last attempt
                    raise  # Re-raise on final attempt
                await asyncio.sleep(min(2 ** attempt, 10))  # True exponential backoff with 10s cap
    except Exception as e:
        logger.error(f"Failed to retrieve agent configuration: {str(e)}")
                    
    
    # Get the previous call transcript if there are any previous important calls
    try:
        
        if not backend_url:
            logger.error("BACKEND_URL not found in environment variables")
            return
        
        response = await httpclient.get(
            f"{backend_url}/userRecord/userid/{user_id}",
            headers={"X-Request-ID": f"{agent_id}-{uuid.uuid4()}"}
        )
        response.raise_for_status() 
        
        logger.info(f"response: {response.json()}")
        
        
        if len(response.json()) > 0:
            user_record = response.json()[0]
            logger.info(f"user_record: {user_record}")
            
            # get the user data from the user_record by using the user_data array in agent_config
            userdata_variables = agent_config["userdata_variables"]
            logger.info(f"userdata_variables: {userdata_variables}")
            userdata = user_record["input_data"]
            userdata_template = {}
            for variable in userdata_variables:
                userdata_template[variable] = ""
                if variable in userdata.keys():
                    userdata_template[variable] = userdata[variable]
            logger.info(f"userdata_template: {userdata_template}")
            
            #add the userdata_template to the agent_config["system_prompt"]
            system_prompt = agent_config["system_prompt"]
            final_system_prompt = system_prompt.format(**userdata_template)
            logger.info(f"final_system_prompt: {final_system_prompt}")
            
            class Assistant(Agent):
                def __init__(self) -> None:
                    super().__init__(instructions=final_system_prompt)

            
            # add the transcript of the previous calls
            # Safely access previous_calls with a default empty list
            previous_calls = user_record.get("previous_important_calls", [])
            logger.info(f"previous_calls: {previous_calls}")   
                
            # Only proceed if previous_calls exists and is not empty
            if previous_calls and len(previous_calls) > 0:
                for c in range(len(previous_calls)):
                    try:
                        logger.info(f"Fetching call transcript for: {previous_calls[c]}")
                        call_response = await httpclient.get(
                            f"{backend_url}/callAnalysis/analysis/{previous_calls[c]}",
                            headers={"X-Request-ID": f"{agent_id}-{uuid.uuid4()}"}
                        ) 
                        call_record = call_response.json()
                        
                        # Check if transcript exists in the response
                        if 'transcript' in call_record:
                            logger.info(f"Got transcript for call: {previous_calls[c]}")
                            if c == 0:
                                agent_config["system_prompt"] += f"""\n\nHere are the Previous Call Transcripts, continue the call with the user from where the previous call ended:
                                {call_record['transcript']}"""
                            else:
                                agent_config["system_prompt"] += f"\n{call_record['transcript']}"
                            logger.info(f"agent_config['system_prompt']: {agent_config['system_prompt']}")
                        else:
                            logger.warning(f"No transcript found in call record for: {previous_calls[c]}")
                    except Exception as inner_e:
                        logger.error(f"Error fetching transcript for call {previous_calls[c]}: {str(inner_e)}")
                        continue  # Skip this call and continue with others
    
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching user record: {e.response.status_code} - {e.response.text}")
    except httpx.RequestError as e:
        logger.error(f"Request error fetching user record: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error fetching user record: {str(e)}")
    
    # Generate a unique filename with timestamp and participant identity 
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # s3_unique_filename = f"{ctx.room.name}_{sid}_{timestamp}"
    s3_unique_filename = f"{user_id}_{timestamp}"

    # Set up recording AFTER room is created and participant has joined
    req = api.RoomCompositeEgressRequest(
        room_name=ctx.room.name,
        layout="speaker",
        audio_only=True,
        file_outputs=[api.EncodedFileOutput(
            filepath=s3_unique_filename,
            disable_manifest=True,
            s3=api.S3Upload(
                access_key=os.getenv("AWS_ACCESS_KEY_ID"),
                secret=os.getenv("AWS_SECRET_ACCESS_KEY"),
                bucket=os.getenv("AWS_S3_BUCKET"),
                region=os.getenv("AWS_REGION", "ap-south-1"),
            ),
        )],
    )
    
    lkapi = api.LiveKitAPI()
    s3_url = ""
    try:
        res = await lkapi.egress.start_room_composite_egress(req)
        logger.info(f"Started room recording: {res.egress_id}")
        
        bucket_name = os.getenv("AWS_S3_BUCKET")
        region = os.getenv("AWS_REGION", "ap-south-1")
        s3_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{s3_unique_filename}.ogg"
        
        logger.info(f"S3 URL for recording: {s3_url}")
        
    except Exception as e:
        logger.error(f"Failed to start room recording: {e}")
        # Continue even if recording fails - don't block the conversation
    
    call_end_time = datetime.now()  
    
    
    await ctx.connect()
    session = AgentSession(
        stt=deepgram.STT(model="nova-2-general", language="hi"),
        llm=openai.LLM(model="gpt-4o-mini"),
        # llm=groq.LLM(
        #     model="llama-3.1-8b-instant"
        # ),
        tts=elevenlabs.TTS(
            voice_id="NeDTo4pprKj2ZwuNJceH",
            model="eleven_flash_v2_5",
            chunk_length_schedule=[50, 100, 200, 260],
        ),
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_input_options=RoomInputOptions(
            # noise_cancellation=noise_cancellation.BVC(),
        ),
    )

    await session.generate_reply(
        instructions="Greet the user and offer your assistance."
    )
    
    # Initialize cumulative metrics dictionary
    cumulative_metrics = {
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        # "stt_duration": 0.0,
        "stt_audio_duration": 0.0,
        "tts_characters_count": 0,
        # "tts_duration": [],  
        "tts_audio_duration": 0.0,
        "end_of_utterance_delay": [],
        "transcription_delay": [],
        "llm_ttft": [],
        "tts_ttfb": [],
        # "vad_inference_count": [],
        # "vad_inference_duration_total": [],
        "end_of_utterance_delay_avg": 0,
        "transcription_delay_avg": 0,
        "llm_ttft_avg": 0,
        "tts_ttfb_avg": 0
    }
    
    usage_collector = metrics.UsageCollector()
    
    @session.on("metrics_collected")
    def _on_metrics_collected(agent_metrics: MetricsCollectedEvent):

        nonlocal cumulative_metrics  # Make the dictionary nonlocal

        # Access the actual metrics object
        metric_data = agent_metrics.metrics

        # Check the type of the metrics data and update cumulative values
        # if isinstance(metric_data, metrics.PipelineVADMetrics):
        #     cumulative_metrics["vad_inference_count"].append(metric_data.inference_count)
        #     cumulative_metrics["vad_inference_duration_total"].append(metric_data.inference_duration_total)
        if isinstance(metric_data, metrics.EOUMetrics):
            cumulative_metrics["end_of_utterance_delay"].append(metric_data.end_of_utterance_delay)
            cumulative_metrics["transcription_delay"].append(metric_data.transcription_delay)
        elif isinstance(metric_data, metrics.LLMMetrics):
            cumulative_metrics["llm_ttft"].append(metric_data.ttft)
            cumulative_metrics["llm_prompt_tokens"] += metric_data.prompt_tokens
            cumulative_metrics["llm_completion_tokens"] += metric_data.completion_tokens
            # logger.info(f"LLM Metrics collected: prompt={metric_data.prompt_tokens}, completion={metric_data.completion_tokens}")
        elif isinstance(metric_data, metrics.STTMetrics):
            # cumulative_metrics["stt_duration"] += metric_data.duration
            cumulative_metrics["stt_audio_duration"] += metric_data.audio_duration
            # logger.info(f"STT Metrics collected: duration={metric_data.duration}, audio_duration={metric_data.audio_duration}")
        elif isinstance(metric_data, metrics.TTSMetrics):
            cumulative_metrics["tts_ttfb"].append(metric_data.ttfb)
            cumulative_metrics["tts_characters_count"] += metric_data.characters_count
            # cumulative_metrics["tts_duration"].append(metric_data.duration)
            cumulative_metrics["tts_audio_duration"] += metric_data.audio_duration
        
    async def log_usage():
        summary = usage_collector.get_summary()
        logger.info(f"Usage: {summary}")
        logger.info(f"Cumulative Metrics: {cumulative_metrics}")
        
    ctx.add_shutdown_callback(log_usage)

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))