# 🤖 Liza: Raspberry Pi AI Voice Assistant & Interactive Tutor

A local-first, multimodal AI assistant designed for the Raspberry Pi. Liza combines a physical touchscreen interface with custom animated emotional states, hardware push-button triggers, speech recognition, and a **Dual-Brain architecture** allowing you to seamlessly toggle between cloud/local LLMs (Groq/Ollama) and a custom AWS EC2 backend.

---

## ✨ Key Features

* **Dual-Brain Architecture:** Toggle between **LIZA** (Groq / Local Ollama) and your custom **AWS Bot** directly from the UI.
* **Interactive Study Modes:**
  * **Tutor Mode:** Structured, expert-level explanations broken down by core principles, mechanisms, and real-world examples.
  * **Co-Tell Mode:** Collaborative partner mode that uses short prompts and asks follow-up questions to test your knowledge.
  * **Re-Tell Mode:** Step-by-step active examiner mode that listens as you explain a topic, validates your statements, and offers feedback.
* **Physical Hardware Support:** Designed for 5-inch touchscreens (XPT2046 SPI) and supports physical GPIO push-buttons for instant wake-up.
* **Live Web Search Fallback:** Automatically queries DuckDuckGo for real-time technical answers when needed.

---

## 🛠️ Hardware Requirements

* Raspberry Pi (Pi 4 or Pi 5 recommended)
* 5-inch HDMI Touchscreen (with SPI touch controller)
* USB Microphone & Speaker (or a combined USB audio device)
* Momentary push-button (optional, for physical wake-up)
* External USB Pendrive/SSD (optional, for storing local Ollama models)

---

## 📦 Dependency Installation Guide

Follow these steps to set up the software environment on a fresh Raspberry Pi OS installation.

### Step 1: System Updates & Dependencies
Open your terminal and ensure your system packages are up to date, then install essential system dependencies for audio and graphics:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-tk python3-pil python3-pil.imagetk portaudio19-dev libasound2-dev -y
