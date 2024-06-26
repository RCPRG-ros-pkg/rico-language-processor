# encoding: utf-8

import json
import os
import subprocess
import urllib2
import jsonschema
from stories.detect_intent import get_detect_intent_system_story
from stories.initiate_conv import get_intiate_conv_system_story
from stories.detect_intent_during_task import get_detect_intent_during_task_system_story
from stories.guess_actor import get_guess_actor_system_story
from stories.fallback_get_fulfilling_question import fallback_get_fulfulling_question_system_story
from stories.detect_unexpected_question import detect_unexpected_question_system_story
from datetime import datetime
from copy import deepcopy

OPENAI_API_KEY = os.environ['OPENAI_API_KEY']

response_schema_base = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "enum": [], # these are created dynamically
        },
        "parameters": {
            "type": "object",
            "properties": {
                # these are created dynamically
            },
            "required": [],
            "additionalProperties": False
        },
        "fulfilling_question": {"type": "string"},
    },
    "required": ["name", "parameters"],
    "additionalProperties": False
}

response_schema_during_task = {
    "type": "object",
    "properties": {
        "name": {
            "type": ["string", "null"],
            "enum": [None], # these are created dynamically
        },
        "unexpected_question": {
            "type": ["string", "null"],
        }
    },
    "required": ["name", "unexpected_question"],
    "additionalProperties": False
}

# can be one of:
# 1. null
# 2. {
#     "answer": string
# }
# 3. {
#     "new_parameter_name": string
# }
response_schema_detect_unexpected_question = {
    "type": ["object", "null"],
    "properties": {
        "answer": {"type": "string"},
        "new_parameter_name": {"type": "string"}
    },
    "additionalProperties": False
}

class OpenAIInterface:
    def __init__(self):
        curr_file_dir = os.path.dirname(os.path.abspath(__file__))
        self.python3_path = os.path.normpath(os.path.join(
            curr_file_dir, '..', 'python3', 'venv', 'bin', 'python'))
        self.scripts_paths = {
            'detect_intent': os.path.normpath(os.path.join(
                curr_file_dir, '..', 'python3', 'detect_intent.py')),
            'initiate_conversation': os.path.normpath(os.path.join(
                curr_file_dir, '..', 'python3', 'initiate_conversation.py')),
            'detect_intent_during_task': os.path.normpath(os.path.join(
                curr_file_dir, '..', 'python3', 'detect_intent_during_task.py')),
        }
        self.api_url = 'https://api.openai.com/v1/chat/completions';
        # self.script_path = os.path.normpath(os.path.join(
        #     curr_file_dir, '..', 'python3', 'script.py'))

    def prepare_response_schema(self, intents_with_params, matched_intent_name):
        response_schema = deepcopy(response_schema_base)
        matched_intent = None
        for intent in intents_with_params:
            response_schema['properties']['name']['enum'].append(unicode(intent['name'], 'utf-8'))
            if intent['name'] == matched_intent_name:
                matched_intent = intent

        if matched_intent is None:
            raise Exception("Intent with name {} not found in intents_with_params".format(matched_intent_name))

        for param in matched_intent['parameters']:
            response_schema['properties']['parameters']['properties'][param] = {
                "type": ["string", "null"]}
            response_schema['properties']['parameters']['required'].append(param)

        return response_schema
    
    def prepare_response_schema_during_task(self, intents_with_description):
        response_schema = deepcopy(response_schema_during_task)
        for intent in intents_with_description:
            response_schema['properties']['name']['enum'].append(unicode(intent['name'], 'utf-8'))
        return response_schema
    
    def initiate_conversation(self, history_events):
        script_path = self.scripts_paths['initiate_conversation']

        history_of_events = []
        messages = []

        for idx, event in enumerate(history_events):
            history_of_events.append("""
            %i.
            actor: %s
            action: %s
            complement: %s
            description: %s
            """ % (idx + 1, event.actor, event.action, event.complement, event.description if event.description else ''))

        last_event = history_events[-1]

        print last_event
        
        is_last_user_saying = last_event.action == 'say' and last_event.actor != 'Rico'

        if is_last_user_saying:
            messages.append({'role': 'user', 'content': last_event.complement})
            history_of_events = history_of_events[:-1]

        history_of_events_string = ''.join(history_of_events)

        history_length = len(history_of_events)

        messages.insert(0, {"role": "system", "content": get_intiate_conv_system_story(history_of_events_string, history_length) })

        response = self.request_gpt("gpt-4-1106-preview", messages)

        return response


    def detect_intent_with_params(self, intents_with_params, conv_history_string, places_names):
        print intents_with_params
        intents_list_string = ''.join(map(lambda (idx, dic): """
        %i. %s
            %s""" % (idx + 1, dic['name'], ('parameters: ' + ','.join(dic['parameters'])) if len(dic['parameters']) else 'no parameters'), list(enumerate(
            intents_with_params
            ))
        ))

        places_string = ', '.join(places_names)

        print(places_string)

        messages = [{
            "role": "system",
            "content": get_detect_intent_system_story(intents_list_string, conv_history_string, places_string)
        }]

        response = self.request_gpt("gpt-4-1106-preview", messages, force_json=True)

        response_dict = None

        try:
            # convert from unicode to normal (\u0119 -> ę)
            response = json.dumps(json.loads(response), ensure_ascii=False)

            print(response)

            response_dict = json.loads(response)

            if isinstance(response_dict, dict):
                print "Validating response..."
                response_schema = self.prepare_response_schema(intents_with_params, response_dict['name'].encode('utf-8'))
                print "Response schema: ", response_schema
                try:
                    jsonschema.validate(response_dict, response_schema)
                except Exception as e:
                    print "Validation failed: ", e
                    # If validation fails, modify the original dictionary and remove invalid parameters
                    error_path = list(e.relative_schema_path)
                    wrong_params = error_path[1] == "parameters"
                    if wrong_params:
                        unnesessary_params = error_path[2] == "additionalProperties"
                        if unnesessary_params:
                            params_from_response = response_dict["parameters"].keys()
                            params_from_schema = response_schema["properties"]["parameters"]["properties"].keys()
                            invalid_params = set(params_from_response) - set(params_from_schema)
                            for invalid_param in invalid_params:
                                del response_dict["parameters"][invalid_param]

                        # Set missing required parameters to null
                        required_params = response_schema["properties"]["parameters"]["required"]
                        for required_param in required_params:
                            if required_param not in response_dict["parameters"]:
                                response_dict["parameters"][required_param] = None

                        # Print the modified dictionary
                        print("Invalid dictionary: ", response_dict)
                    else:
                        raise e
                else:
                    # If validation succeeds, do something with the original dictionary
                    print("Valid dictionary: ", response_dict)

        except:
            print("OpenAI response is not a valid JSON. Falling back to empty response.")
            print(response)
            response_dict = None

        return response_dict

    def detect_intent_during_task(self, history_events, intents_with_description, last_user_message, curr_task_params, curr_actor):
        print intents_with_description
        intents_list_string = ''.join(map(lambda (idx, dic): """
        %i. %s
            %s""" % (idx + 1, dic['name'], ('description: ' + dic['description']) if len(dic['description']) else 'no description'), list(enumerate(
            intents_with_description
            ))
        ))

        curr_task_params_string = ', '.join(curr_task_params)

        history_of_events = []

        for idx, event in enumerate(history_events):
            history_of_events.append("""
            %i.
            actor: %s
            action: %s
            complement: %s
            description: %s
            """ % (idx + 1, event.actor, event.action, event.complement, event.description if event.description else ''))

        history_events_string = ''.join(history_of_events)

        print "Intents list: ", intents_list_string
        print "History events: ", history_events_string
        print "Last user message: ", last_user_message

        messages = [
            {"role": "system", "content": get_detect_intent_during_task_system_story(history_events_string, intents_list_string, last_user_message, curr_task_params_string, curr_actor) },
        ]

        response = self.request_gpt("gpt-4-1106-preview", messages)

        intents_names = list(map(lambda intent: intent['name'], intents_with_description))

        if response in intents_names:
            return response
        else:
            return None
    
    def detect_unexpected_question(self, history_events, curr_actor, last_user_message):
        history_of_events = []

        for idx, event in enumerate(history_events):
            history_of_events.append("""
            %i.
            actor: %s
            action: %s
            complement: %s
            description: %s
            """ % (idx + 1, event.actor, event.action, event.complement, event.description if event.description else ''))

        history_events_string = ''.join(history_of_events)

        print "History events: ", history_events_string

        messages = [
            {"role": "system", "content": detect_unexpected_question_system_story(history_events_string, curr_actor) },
            {"role": "user", "content": last_user_message }
        ]

        response = self.request_gpt("gpt-4-1106-preview", messages, force_json=True)

        response_dict = None

        try:
            # convert from unicode to normal (\u0119 -> ę)
            response = json.dumps(json.loads(response), ensure_ascii=False)

            print(response)

            response_dict = json.loads(response)

            print "Validating response..."
            response_schema = response_schema_detect_unexpected_question
            print "Response schema: ", response_schema
            try:
                jsonschema.validate(response_dict, response_schema)
            except Exception as e:
                raise e
            else:
                # If validation succeeds, do something with the original dictionary
                print("Valid dictionary: ", response_dict)

        except:
            print("OpenAI response is not a valid JSON. Falling back to empty response.")
            print(response)
            response_dict = None

        return response_dict

    def guess_actor(self, history_events, last_user_message):
        history_of_events = []

        for idx, event in enumerate(history_events):
            history_of_events.append("""
            %i.
            actor: %s
            action: %s
            complement: %s
            description: %s
            """ % (idx + 1, event.actor, event.action, event.complement, event.description if event.description else ''))

        history_events_string = ''.join(history_of_events)

        print "History events: ", history_events_string
        print "Last user message: ", last_user_message

        number_of_events = len(history_events)

        messages = [
            {"role": "system", "content": get_guess_actor_system_story(history_events_string, number_of_events) },
            {"role": "user", "content": last_user_message }
        ]

        response = self.request_gpt("gpt-3.5-turbo", messages)

        lowercased_response = response.lower()

        if 'keeper' in lowercased_response:
            return 'keeper'
        
        if 'senior' in lowercased_response:
            return 'senior'
        
        return 'unknown'

    def fallback_get_fulfilling_question(self, curr_intent, unretrieved_parameters):
        unretrieved_parameters_string = '"' + '", "'.join(unretrieved_parameters) + '"'

        messages = [
            {"role": "system", "content": fallback_get_fulfulling_question_system_story(curr_intent, unretrieved_parameters_string) }
        ]

        response = self.request_gpt("gpt-3.5-turbo", messages)

        return response
    
    def request_gpt(self, model, messages, force_json=False):
        print 'REQUEST', messages

        req_params = {
            "model": model,
            "messages": messages,
            "max_tokens": 1000,
            "temperature": 0,
            "top_p": 1,
            "frequency_penalty": 0,
            "presence_penalty": 0,
        }

        if force_json:
            req_params['response_format'] = { "type": "json_object" }

        request = urllib2.Request(self.api_url, json.dumps(req_params), headers={"Authorization": "Bearer " + OPENAI_API_KEY, "Content-Type": "application/json"})

        completion = urllib2.urlopen(request).read()

        print "Completion: ", completion

        json_response = json.loads(completion)

        print "JSON response: ", json_response

        response = json_response['choices'][0]['message']['content']

        print("OpenAI response: ", response)

        self.log_request(req_params, response)

        return response

    def log_request(self, req_params, response_content):
        log_folder = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'logs'))
        if not os.path.exists(log_folder):
            os.makedirs(log_folder)
        log_filename = "{}_{:%Y%m%d_%H%M%S}.log".format(req_params['model'], datetime.now())

        with open(os.path.join(log_folder, log_filename), 'w') as f:
            f.write("REQUEST PARAMS\n")
            json_string = json.dumps(req_params, indent=4)
            json_string = json_string.replace("\\n", "\n")
            f.write(json_string)
            f.write("\n\n")
            f.write("RESPONSE\n")
            f.write(response_content)
            f.write("\n\n")
        
        print("Logged request to {}".format(log_filename))
