# Patient AI Voice Agent

An AI-powered healthcare voice assistant that handles real-time patient conversations over phone calls using Twilio, Flask, OpenAI, and MongoDB.

The system is designed to assist patients with common appointment-related tasks such as booking, checking, cancelling, and rescheduling appointments through a conversational voice interface.

---

## 🚀 Project Overview

The Patient AI Voice Agent combines:

- AI-powered natural language processing
- Voice communication through Twilio
- Flask-based webhook handling
- OpenAI for conversational responses
- MongoDB for persistent data storage
- Rule-based transaction flows for appointment management
- Date and time extraction using Dateparser
- Ngrok for local development and webhook exposure

The application provides a conversational interface where users can interact with a virtual patient-services assistant over a phone call.

---

## 🎯 Key Features

### 📞 Voice Interaction

- Handles inbound phone calls using Twilio
- Supports outbound calls
- Converts speech input into text
- Generates spoken responses using Twilio Text-to-Speech
- Supports `en-IN` language configuration

### 🏥 Appointment Management

The agent supports:

- Book appointment
- Check appointment
- Cancel appointment
- Reschedule appointment

### 👨‍⚕️ Doctor Management

The system can:

- Store doctor information
- Search doctors by name
- Search doctors by specialty
- Display doctor fees
- Display doctor availability
- Suggest doctors based on patient concerns

### 🧠 AI Conversation

OpenAI is used to handle natural-language conversations when rule-based processing is not sufficient.

The system maintains conversation history and uses the current transaction state to provide context-aware responses.

### 💾 MongoDB Persistence

MongoDB is used to store:

- Doctor information
- Appointments
- Call logs
- Session history
- Embedding metadata
- Audio metadata

### 🔄 Local JSON Fallback

The application can also work with local JSON storage when MongoDB is not configured.

### 🌐 Ngrok Integration

Ngrok can expose the local Flask application to the internet so that Twilio can reach the webhook endpoints during development.

---

# 🏗️ Architecture

```text
                    ┌─────────────────┐
                    │     Patient     │
                    │  Phone / Voice  │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │     Twilio      │
                    │ Voice Platform   │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │  Flask Server   │
                    │  /voice         │
                    │  /process       │
                    └────────┬────────┘
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
           ┌────────────────┐  ┌─────────────────┐
           │ Rule-Based     │  │ OpenAI LLM      │
           │ Conversation   │  │ Processing      │
           │ State Machine  │  │                 │
           └────────┬───────┘  └────────┬────────┘
                    │                   │
                    └─────────┬─────────┘
                              ▼
                    ┌─────────────────┐
                    │   Appointment   │
                    │     Logic       │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │    MongoDB      │
                    │   Persistence   │
                    └─────────────────┘
