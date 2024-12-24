import asyncio
import uuid
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.vectorstores import FAISS
from langchain.chains.question_answering import load_qa_chain
from langchain.prompts import PromptTemplate
import torch
from transformers import pipeline, LlamaForCausalLM, LlamaTokenizer
import os
from dotenv import load_dotenv
import transformers
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict, Any

load_dotenv()

# Load and split the text file
with open("general.txt", encoding="utf8") as f:
    raw_text = f.read()

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=100,
    length_function=len,
)
text_chunks = text_splitter.split_text(raw_text)


embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
vector_store = FAISS.from_texts(text_chunks, embedding=embeddings)
vector_store.save_local("faiss_index")

bnb_config = transformers.BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type='nf4',
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16
)


model_id = "meta-llama/Llama-3.2-11B-Vision-Instruct"
model_config = transformers.AutoConfig.from_pretrained(model_id)

model = transformers.AutoModelForCausalLM.from_pretrained(
    model_id,
    trust_remote_code=True,
    config=model_config,
    quantization_config=bnb_config,
    cache_dir='models'
)

model.eval()

tokenizer = transformers.AutoTokenizer.from_pretrained(model_id)
text_generator = pipeline("text-generation", model=model, tokenizer=tokenizer)

from langchain.llms import HuggingFacePipeline

def get_conversational_chain():
    prompt_template = """
    You are an assistant for Amrita University. Use the following pieces of information to help answer the user's questions.
    Always maintain context from the previous conversation and combine it with new information from the knowledge base.

    Previous Conversation:
    {chat_history}

    Context from knowledge base:
    {context}
    
    Current Question: {question}
    
    If you previously provided information that's relevant to the current question, use that information along with any new context.
    If you cannot find specific information in the current context but you mentioned it in the chat history, you can refer to that.
    If you truly don't have enough information to answer, acknowledge what you know and what you don't know.
    """
    prompt = PromptTemplate(
        template=prompt_template, 
        input_variables=["context", "question", "chat_history"]
    )

    # Wrap the Hugging Face pipeline
    llm = HuggingFacePipeline(pipeline=text_generator)
    chain = load_qa_chain(llm, chain_type="stuff", prompt=prompt)
    return chain

def format_chat_history(history):
    formatted_history = ""
    for entry in history:
        if "user" in entry:
            formatted_history += f"Human: {entry['user']}\n"
        if "bot" in entry:
            formatted_history += f"Assistant: {entry['bot']}\n"
    return formatted_history

def answer_question(user_question, chat_history):
    new_db = FAISS.load_local("faiss_index", embeddings,allow_dangerous_deserialization=True)

    # Create a combined query using recent context
    context_query = user_question
    if chat_history:
        last_exchange = chat_history[-2:]  # Get last user question and bot response
        context_query = f"{' '.join([msg.get('user', msg.get('bot', '')) for msg in last_exchange])} {user_question}"
    
    docs = new_db.similarity_search(context_query)
    
    chain = get_conversational_chain()
    formatted_history = format_chat_history(chat_history[-4:])  # Keep last 2 exchanges for context
    
    response = chain(
        {
            "input_documents": docs, 
            "question": user_question,
            "chat_history": formatted_history
        }, 
        return_only_outputs=True
    )
    
    return response["output_text"]

conversation_context: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}

class QueryRequest(BaseModel):
    session_id: str | None = None
    input_text: str

app = FastAPI()

@app.get("/")
def index():
    return {"message": "Hello! Use the /get-response endpoint to chat."}

@app.post("/get-response/")
def get_response(request: QueryRequest):
    session_id = request.session_id if request.session_id else str(uuid.uuid4())

    user_input = request.input_text

    if session_id not in conversation_context:
        conversation_context[session_id] = {"history": []}

    conversation_history = conversation_context[session_id]["history"]
    conversation_history.append({"user": user_input})

    response = answer_question(user_input, conversation_history)

    conversation_history.append({"bot": response})

    conversation_context[session_id]["history"] = conversation_history

    return {"session_id": session_id, "response": response, "history": conversation_history}

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
