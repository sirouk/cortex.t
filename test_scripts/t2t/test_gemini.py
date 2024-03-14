import os
import google.generativeai as genai
import traceback
import asyncio

google_api = os.environ.get('GOOGLE_API_KEY')
genai.configure(api_key=google_api)

# https://ai.google.dev/tutorials/python_quickstart
model = 'gemini-pro'
messages = [
    {
        "role": "system",
        "content": "respond in spanish with at least 10 words, write a paragraph"
    },
    {
        "role": "user",
        "content": "Hello!"
    }
]
# messages = ', '.join(message['content'] for message in messages)
temperature = 0
max_tokens = 200
top_p = 1
top_k = 1
seed = 10

# for m in genai.list_models():
#   if 'generateContent' in m.supported_generation_methods:
#     print(m.name)
# model = genai.GenerativeModel(model)

# Streaming
async def call_gemini(messages, temperature, model, max_tokens, top_p, top_k):
    print(f"Calling Gemini. Temperature = {temperature}, Model = {model}, Messages = {messages}")
    try:
        model = genai.GenerativeModel(model)
        stream = model.generate_content(
            str(messages),
            stream=True,
            generation_config=genai.types.GenerationConfig(
                candidate_count=1,
                stop_sequences=['x'],
                temperature=temperature,
                max_output_tokens=max_tokens,
                top_p=top_p,
                top_k=top_k,
                # seed=seed,
            )
        )
        for chunk in stream:
            try:
                print(chunk.text, end="", flush=True)
            except:
                pass
        # print(f"validator response is {stream.text}")
        return stream.text
    except:
        print(f"error in call_gemini {traceback.format_exc()}")

# Non streaming
# async def call_gemini(messages, temperature, model, max_tokens, top_p, top_k):
#     print(f"Calling Gemini. Temperature = {temperature}, Model = {model}, Messages = {messages}")
#     try:
#         model = genai.GenerativeModel(model)
#         response = model.generate_content(
#             str(messages),
#             stream=False,
#             generation_config=genai.types.GenerationConfig(
#                 candidate_count=1,
#                 stop_sequences=['x'],
#                 temperature=temperature,
#                 max_output_tokens=max_tokens,
#                 top_p=top_p,
#                 top_k=top_k,
#                 # seed=seed,
#             )
#         )

#         print(f"validator response is {response.text}")
#         return response.text
#     except:
#         print(f"error in call_gemini {traceback.format_exc()}")

async def main():
    answer = await call_gemini(messages, temperature, model, max_tokens, top_p, top_k)
    print(f"\nAnswer = {answer}")

if __name__ == "__main__":
    asyncio.run(main())


# from PIL import Image

# img = Image.open('image.jpg')
# # img.show()

# # Initialize and use the model
# model = genai.GenerativeModel('gemini-pro-vision')
# response = model.generate_content(img)

# print(response.text)
