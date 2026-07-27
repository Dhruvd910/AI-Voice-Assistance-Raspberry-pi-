Piper TTS Configuration & Voice Models
Piper provides fast, local neural speech synthesis optimized for ARM devices like the Raspberry Pi without requiring cloud servers or subscriptions.

1. Create Models Directory
Bash
mkdir -p models/piper
cd models/piper
2. Download Piper Voice Models
Download your desired voice model (.onnx) and its accompanying config (.onnx.json) from the official https://github.com/OHF-Voice/piper1-gpl

Example using en_US-lessac-medium (Natural US English):

Bash
# Download ONNX Voice Model
wget [https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx](https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx)

# Download Config File (Required alongside model)
wget [https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json](https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json)

cd ../..
3. Verify Piper Installation
Test Piper from the command line or terminal to confirm audio playback works:

Bash
echo "Hello! Piper text to speech is running on your Raspberry Pi." | piper --model models/piper/en_US-lessac-medium.onnx --output_file test.wav
aplay test.wav
🔑 Environment Setup
Create a .env file in the project root folder:

Code snippet
GROQ_API_KEY=your_groq_api_key_here
PIPER_MODEL_PATH=models/piper/en_US-lessac-medium.onnx
📁 Directory Structure
Plaintext
rpi-voice-assistant/
├── models/
│   └── piper/
│       ├── en_US-lessac-medium.onnx
│       └── en_US-lessac-medium.onnx.json
├── main.py
├── requirements.txt
├── .env
└── README.md
