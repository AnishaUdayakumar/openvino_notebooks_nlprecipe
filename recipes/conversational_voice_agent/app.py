import argparse
from pathlib import Path
from typing import Tuple, List

import gradio as gr
import librosa
import numpy as np
import time
import torch
import intel_extension_for_pytorch as ipex
from datasets import load_dataset
from optimum.intel import OVModelForCausalLM, OVModelForSpeechSeq2Seq
from transformers import AutoConfig, AutoTokenizer, AutoProcessor, SpeechT5Processor, SpeechT5ForTextToSpeech, SpeechT5HifiGan, PreTrainedTokenizer

# Import nltk and download the punkt tokenizer models
import nltk
nltk.download('punkt', quiet=True)

# Global variables initialization
AUDIO_WIDGET_SAMPLE_RATE = 16000
USE_IPEX = True  # Globally control the use of IPEX optimizations
SYSTEM_CONFIGURATION = "You're Adrishuo - a conversational agent. You talk to a customer. You work for a car dealer called XYZ. Your task is to recommend the customer a car based on their needs."
GREET_THE_CUSTOMER = "Please introduce yourself and greet the customer"
NEURAL_CHAT_MODEL_TEMPLATE = ("{% if messages[0]['role'] == 'system' %}"
                              "{% set loop_messages = messages[1:] %}"
                              "{% set system_message = messages[0]['content'] %}"
                              "{% else %}"
                              "{% set loop_messages = messages %}"
                              "{% set system_message = 'You are a helpful, respectful and honest assistant. Always answer as helpfully as possible, while being safe. "
                              "Your answers should not include any harmful, unethical, racist, sexist, toxic, dangerous, or illegal content. Please ensure that your responses are socially unbiased and positive in nature. "
                              "If a question does not make any sense, or is not factually coherent, explain why instead of answering something not correct. If you don\\'t know the answer to a question, please don\\'t share false information.' %}"
                              "{% endif %}"
                              "{{ '### System:\\n' + system_message.strip() + '\\n' }}"
                              "{% for message in loop_messages %}"
                              "{% if (message['role'] == 'user') != (loop.index0 % 2 == 0) %}"
                              "{{ raise_exception('Conversation roles must alternate user/assistant/user/assistant/...') }}"
                              "{% endif %}"
                              "{% set content = message['content'] %}"
                              "{% if message['role'] == 'user' %}"
                              "{{ '### User:\\n' + content.strip() + '\\n' }}"
                              "{% elif message['role'] == 'assistant' %}"
                              "{{ '### Assistant:\\n' + content.strip() + '\\n'}}"
                              "{% endif %}"
                              "{% endfor %}"
                              )

# Initialize Model variables
chat_model: OVModelForCausalLM = None
chat_tokenizer: PreTrainedTokenizer = None
message_template: str = None
asr_model: OVModelForSpeechSeq2Seq = None
asr_processor: AutoProcessor = None
tts_processor: SpeechT5Processor = None
tts_model: SpeechT5ForTextToSpeech = None
tts_vocoder: SpeechT5HifiGan = None
speaker_embeddings = None


def load_asr_model(model_dir: Path) -> None:
    """
    Load automatic speech recognition model and assign it to a global variable

    Params:
        model_dir: dir with the ASR model
    """
    global asr_model, asr_processor

    # create a distil-whisper model and its processor
    asr_model = OVModelForSpeechSeq2Seq.from_pretrained(model_dir, device="AUTO")
    asr_processor = AutoProcessor.from_pretrained(model_dir)


# Function to load SpeechT5 models with IPEX optimization
def load_tts_model() -> None:
    """
    Loads the Text-to-Speech (TTS) models and processor for SpeechT5 with optional IPEX optimization.

    Params:
        use_ipex: A boolean flag to determine if IPEX optimization should be used.
    """
    global tts_processor, tts_model, tts_vocoder, speaker_embeddings

    tts_processor = SpeechT5Processor.from_pretrained("microsoft/speecht5_tts")
    tts_model = SpeechT5ForTextToSpeech.from_pretrained("microsoft/speecht5_tts")
    tts_vocoder = SpeechT5HifiGan.from_pretrained("microsoft/speecht5_hifigan")

    if USE_IPEX:
        # Apply IPEX optimization to the models
        tts_model = ipex.optimize(tts_model)
        tts_vocoder = ipex.optimize(tts_vocoder)

    # Load speaker embeddings
    embeddings_dataset = load_dataset("Matthijs/cmu-arctic-xvectors", split="validation")
    speaker_embeddings = torch.tensor(embeddings_dataset[7306]["xvector"]).unsqueeze(0)


def load_chat_model(model_dir: Path) -> None:
    """
    Load chat model and assign it to a global variable

    Params:
        model_dir: dir with the chat model
    """
    global chat_model, chat_tokenizer, message_template

    # load llama model and its tokenizer
    ov_config = {'PERFORMANCE_HINT': 'LATENCY', 'NUM_STREAMS': '1', "CACHE_DIR": ""}
    chat_model = OVModelForCausalLM.from_pretrained(model_dir, device="AUTO", config=AutoConfig.from_pretrained(model_dir), ov_config=ov_config)
    chat_tokenizer = AutoTokenizer.from_pretrained(model_dir)
    # neural chat requires different template than specified in the tokenizer
    message_template = NEURAL_CHAT_MODEL_TEMPLATE if ("neural-chat" in model_dir.name) else chat_tokenizer.default_chat_template


def respond(prompt: str) -> str:
    """
    Respond to the current prompt

    Params:
        prompt: user's prompt
    Returns:
        The chat's response
    """
    start_time = time.time()  # Start time
    # tokenize input text
    inputs = chat_tokenizer(prompt, return_tensors="pt").to(chat_model.device)
    input_length = inputs.input_ids.shape[1]
    # generate response tokens
    outputs = chat_model.generate(**inputs, max_new_tokens=256, do_sample=True, temperature=0.6, top_p=0.9, top_k=50)
    token = outputs[0, input_length:]
    # decode tokens into text
    end_time = time.time()  # End time
    print("Chat model response time: {:.2f} seconds".format(end_time - start_time))
    return chat_tokenizer.decode(token).split("</s>")[0]


def chat(history: List) -> List[List[str]]:
    """
    Chat function. It generates response based on a prompt

    Params:
        history: history of the messages (conversation) so far
    Returns:
        History with the latest chat's response
    """
    # the conversation must be in that format to use chat template
    conversation = [
        {"role": "system", "content": SYSTEM_CONFIGURATION},
        {"role": "user", "content": GREET_THE_CUSTOMER}
    ]
    # add prompts to the conversation
    for user_prompt, assistant_response in history:
        if user_prompt:
            conversation.append({"role": "user", "content": user_prompt})
        if assistant_response:
            conversation.append({"role": "assistant", "content": assistant_response})

    # use a template specific to the model
    conversation = chat_tokenizer.apply_chat_template(conversation, chat_template=message_template, tokenize=False)

    # generate response for the conversation
    response = respond(conversation)
    history[-1][1] = response

    # return chat history as the list of message pairs
    return history


def synthesize(conversation: List[List[str]]) -> Tuple[int, np.ndarray]:
    """
    Synthesizes speech from the last message in a conversation using a TTS model.

    Params:
        conversation: The conversation history.
        use_ipex: Indicates whether IPEX optimization is used for inference.

    Returns:
        A tuple of the audio sample rate and the combined audio numpy array.
    """
    print(f"Running inference with {'IPEX optimization.' if USE_IPEX else 'standard PyTorch.'}")

    start_time = time.time()
    prompt = conversation[-1][-1]
    sentences = nltk.sent_tokenize(prompt)
    audio_segments = []

    # Global speaker embeddings variable
    global speaker_embeddings

    for sentence in sentences:
        inputs = tts_processor(text=sentence, return_tensors="pt")
        speech = tts_model.generate_speech(inputs["input_ids"], speaker_embeddings, vocoder=tts_vocoder)
        audio_segments.append(speech.numpy())

    # Combine audio segments
    combined_audio = np.concatenate(audio_segments, axis=0)

    end_time = time.time()
    print("TTS model synthesis time: {:.2f} seconds".format(end_time - start_time))
    return AUDIO_WIDGET_SAMPLE_RATE, combined_audio


def transcribe(audio: Tuple[int, np.ndarray], conversation: List[List[str]]) -> List[List[str]]:
    """
    Transcribe audio to text

    Params:
        audio: audio to transcribe text from
        conversation: conversation history with the chatbot
    Returns:
        User prompt as a text
    """
    start_time = time.time()  # Start time for ASR process

    sample_rate, audio = audio
    # the whisper model requires 16000Hz, not 44100Hz
    audio = librosa.resample(audio.astype(np.float32), orig_sr=sample_rate, target_sr=AUDIO_WIDGET_SAMPLE_RATE).astype(np.int16)

    # get input features from the audio
    input_features = asr_processor(audio, sampling_rate=AUDIO_WIDGET_SAMPLE_RATE, return_tensors="pt").input_features
    # get output
    predicted_ids = asr_model.generate(input_features)
    # decode output to text
    transcription = asr_processor.batch_decode(predicted_ids, skip_special_tokens=True)[0]

    end_time = time.time()  # End time for ASR process
    print(f"ASR model response time: {end_time - start_time:.2f} seconds")  # Print the ASR processing time

    # add the text to the conversation
    conversation.append([transcription, None])
    return conversation


def create_UI(initial_message: str) -> gr.Blocks:
    """
    Create web user interface

    Params:
        initial_message: message to start with
    Returns:
        Demo UI
    """
    with gr.Blocks(title="Talk to Adrishuo - a conversational voice agent") as demo:
        gr.Markdown("""
        # Talk to Adrishuo - a conversational voice agent

        Instructions for use:
        - record your question/comment using the first audio widget ("Your voice input")
        - wait for the chatbot to response ("Chatbot")
        - wait for the output voice in the last audio widget ("Chatbot voice output")
        """)
        with gr.Row():
            # user's input
            input_audio_ui = gr.Audio(sources=["microphone"], scale=5, label="Your voice input")
            # submit button
            submit_audio_btn = gr.Button("Submit", variant="primary", scale=1)

        # chatbot
        chatbot_ui = gr.Chatbot(value=[[None, initial_message]], label="Chatbot")
        # chatbot's audio response
        output_audio_ui = gr.Audio(autoplay=True, interactive=False, label="Chatbot voice output")

        # events
        submit_audio_btn.click(transcribe, inputs=[input_audio_ui, chatbot_ui], outputs=chatbot_ui)\
            .then(chat, chatbot_ui, chatbot_ui)\
            .then(synthesize, chatbot_ui, output_audio_ui)
    return demo


def run(asr_model_dir: Path, chat_model_dir: Path, public_interface: bool = False) -> None:
    """
    Run the assistant application

    Params
        asr_model_dir: dir with the automatic speech recognition model
        chat_model_dir: dir with the chat model
        tts_model_dir: dir with the text-to-speech model
        public_interface: whether UI should be available publicly
    """
    # load whisper model
    load_asr_model(asr_model_dir)
    # load chat model
    load_chat_model(chat_model_dir)
    # load speecht5 model
    load_tts_model()

    # get initial greeting
    history = chat([[None, None]])
    initial_message = history[0][1]

    # create user interface
    demo = create_UI(initial_message)
    # launch demo
    demo.launch(share=public_interface)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--asr_model_dir', type=str, default="model/distil-large-v2-FP16", help="Path to the automatic speech recognition model directory")
    parser.add_argument('--chat_model_dir', type=str, default="model/llama2-7B-INT8", help="Path to the chat model directory")
    parser.add_argument('--public_interface', default=False, action="store_true", help="Whether interface should be available publicly")

    args = parser.parse_args()
    run(Path(args.asr_model_dir), Path(args.chat_model_dir), args.public_interface)