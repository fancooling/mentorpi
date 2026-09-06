#!/usr/bin/env python3
# encoding: utf-8
# @Author: Aiden
# @Date: 2025/02/28
from config import *
from speech import speech
import time
client = speech.OllamaAPI(ollama_host)

# Additional parameters can be provided in the following way(更多参数采用以下方式进行输入)
# Streaming output(流式输出)
# params = {"model": 'qwen3:1.7b', 
          # "messages": [
            # {
                # "role": "user",
                # "content": '以科技改变生活写一段中文文案, 要求:字数不少于50, 不大于100'
            # },
          # ],
          # "stream": True,
          # "think": False}
# stream = client.llm_origin(params)
# for event in stream:
    # text = event.message.content
    # if text:
        # print(text)

#think
# params = {"model": 'qwen3:1.7b', 
          # "messages": [
            # {
                # "role": "user",
                # "content": '以科技改变生活写一段中文文案, 要求:字数不少于50, 不大于100'
            # },
          # ],
          # "think": True}
# res = client.llm_origin(params)
# print(res.message.thinking)
# print('*******************')
# print(res.message.content)

# print(client.llm('以科技改变生活写一段中文文案, 要求:字数不少于50, 不大于100', prompt='', model='qwen3:0.6b', enable_think=True)) 
PROMPT = '''
##角色任务
你是一辆智能小车，可以通过 x 方向和 y 方向控制线速度,单位m/s，并通过 z 方向控制角速度,单位rad/s，t控制时间单位s。需要根据输入的内容，生成对应的指令。

##要求
1.确保速度范围正确：
	线速度：x, y ∈ [-1.0, 1.0]（负值表示反方向）
	角速度：z ∈ [-1.0, 1.0]（逆时针为正, 顺时针为负）
2.顺序执行多个动作，输出一个 包含多个移动指令的 action 列表，仅在最后一个动作后添加 [0.0, 0.0, 0.0, 0.0] 以确保小车停止。
3.x和y默认为0.2, z默认为1, t默认为2。 
	4.为每个动作序列编织一句精炼（5至10字）、风趣且变化无穷的反馈信息，让交流过程妙趣横生。
5.直接输出json结果，不要分析，不要输出多余内容。
6.格式：
{  
  "action": [[x1, y1, z1, t1], [x2, y2, z2, t2], ..., [0.0, 0.0, 0.0, 0.0]],  
  "response": "xx"  
}  
7.很强的数学计算能力
8.注意漂移是移动和旋转和组合，需要精确的计算, 例如[0.0, -0.2, 1.0, 2.0]

##特别注意
- "action"键下承载一个按执行顺序排列的函数名称字符串数组，当找不到对应动作函数时action输出[]。 
- "response"键则配以精心构思的简短回复，完美贴合上述字数与风格要求。 

##任务示例
输入：向前移动 2 秒，然后顺时针旋转 1 秒
输出：{"action": [[0.2, 0.0, 0.0, 2.0], [0.0, 0.0, -1.0, 1.0], [0,0, 0.0, 0.0, 0.0]], "response": "前进 2 秒，然后顺时针旋转 1 秒，出发！"}
	输入：向前走1米
输出：{"action": [[0.2, 0.0, 0.0, 5.0], [0.0, 0.0, 0.0, 0.0]], "response": "好嘞"}
    '''
user_input = "前进2s,后退4秒，然后左转1s,最后右转2s"

start_time = time.time()
params = {"model": 'qwen3:1.7b', 
          "messages": [
            {
                "role": "system",
                "content": PROMPT
            },
            {
                "role": "user",
                "content": user_input
            },
          ],
          "stream": False,
          "think": False,
          # "options": {
            # "num_keep": 5,
            # "seed": 42,
            # "num_predict": 256,
            # "top_k": 20,
            # "top_p": 0.9,
            # "min_p": 0.0,
            # "typical_p": 0.7,
            # "repeat_last_n": 33,
            # "temperature": 0.8,
            # "repeat_penalty": 1.2,
            # "presence_penalty": 1.5,
            # "frequency_penalty": 1.0,
            # "penalize_newline": True,
            # "stop": ["\n", "user:"],
            # "numa": False,
            # "num_ctx": 2048,
            # "num_batch": 2,
            # "num_gpu": 1,
            # "main_gpu": 0,
            # "use_mmap": True,
            # "num_thread": 4
          # }
        }
print(client.llm_origin(params))
# print(client.llm(user_input, prompt=PROMPT, model='qwen3-fast', num_ctx=2048, enable_think=False)) 
print(time.time() - start_time)

start_time = time.time()
print(client.llm_origin(params))
# print(client.llm(user_input, prompt=PROMPT, model='qwen3-fast', num_ctx=2048, enable_think=False)) 
print(time.time() - start_time)
