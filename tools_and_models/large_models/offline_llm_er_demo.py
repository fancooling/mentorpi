#!/usr/bin/env python3
# encoding: utf-8
# @Author: Aiden
# @Date: 2025/02/28
from config import *
from speech import speech

client = speech.OllamaAPI(ollama_host)

messages = [{"role": "user", "content": '好烦'}]
assistant_output = client.llm_multi_turn(messages, model='qwen3:1.7b')
print(assistant_output)

messages.append({"role": "assistant", "content": assistant_output})

messages.append({"role": "user", "content": '哈哈'})
assistant_output = client.llm_multi_turn(messages, model='qwen3:1.7b')
print(assistant_output)
