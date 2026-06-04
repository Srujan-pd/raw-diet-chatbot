from google import genai
import os
from dotenv import load_dotenv
load_dotenv()
try:
    client = genai.Client(api_key=os.getenv(GEMINI_API_KEY))
    models = client.models.list()
    with open(valid_models.txt, w) as f:
        for m in models:
            f.write(m.name + \n)
    print(Success)
except Exception as e:
    print(Error:, e)
