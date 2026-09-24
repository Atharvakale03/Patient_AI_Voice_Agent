import os 
import re
import json
import tempfile
import uuid
import requests
import datetime
import subprocess
import time
import threading
import hashlib
import math
import shutil
import random 
from flask import Flask, request, send_file, abort, send_from_directory
from twilio.twiml.voice_response import VoiceResponse
from dotenv import load_dotenv
from openai import OpenAI
import dateparser
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import pytz
from typing import Optional, Dict, Any, Tuple, List
from pymongo import MongoClient, ASCENDING
from urllib.parse import quote_plus


# ---------------- Load env ----------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_SID = os.getenv("TWILIO_SID") or os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH") or os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.getenv("TWILIO_PHONE") or os.getenv("TWILIO_PHONE_NUMBER")
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL") or None
OUTBOUND_TARGET_DEFAULT = os.getenv("OUTBOUND_TARGET_NUMBER") or None

MONGO_USER = os.getenv("MONGO_USER")
MONGO_PASS = os.getenv("MONGO_PASS")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "AiAgent")

if MONGO_USER and MONGO_PASS:

    MONGO_URI = f"mongodb+srv://{quote_plus(MONGO_USER)}:{quote_plus(MONGO_PASS)}@aiagent.usvisow.mongodb.net/{MONGO_DB_NAME}?retryWrites=true&w=majority"
else:
    MONGO_URI = None

mongo_client: MongoClient | None = None
mongo_db = None
try:
    if MONGO_URI:
        
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_db = mongo_client[MONGO_DB_NAME]
        print("✅ MongoDB client initialized successfully")
    else:
        print("⚠️ MONGO URI not provided; running without MongoDB persistence.")
except Exception as e:
    print("❌ MongoDB connection failed:", e)
    mongo_client = None
    mongo_db = None

# ---------------- Basic dirs ----------------
DATA_DIR = "./data"
AUDIO_DIR = "static/audio"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)


APPOINTMENT_FILE = os.path.join(DATA_DIR, "appointments.json")
DOCTOR_FILE = os.path.join(DATA_DIR, "doctors.json")
CALL_LOG_FILE = os.path.join(DATA_DIR, "call_logs.json")
AUDIO_METADATA_FILE = os.path.join(DATA_DIR, "audio_metadata.json")
SESSION_HISTORY_FILE = os.path.join(DATA_DIR, "session_history.json")
SESSION_FILE = SESSION_HISTORY_FILE 

# ---------------- FFmpeg Path Setup ----------------

FFMPEG_PATH = r"C:\Users\Admin\Downloads\ffmpeg-8.0-full_build\ffmpeg-8.0-full_build\bin\ffmpeg.exe"


if not shutil.which("ffmpeg"):
    ffmpeg_dir = os.path.dirname(FFMPEG_PATH)   
    if os.path.exists(ffmpeg_dir):
        os.environ["PATH"] += os.pathsep + ffmpeg_dir
        
ffmpeg_found = shutil.which("ffmpeg")
print("🔍 FFmpeg path in use:", ffmpeg_found)

if ffmpeg_found:
    print("✅ FFmpeg found at:", ffmpeg_found)
else:
    print("❌ FFmpeg still not found! Check FFMPEG_PATH variable.")

E164_REGEX = re.compile(r"^\+[1-9]\d{1,14}$")

def is_valid_e164(number):
    """Checks if a number is in E.164 format."""
    return bool(E164_REGEX.match(number))

# ---------------- Twilio client -----------------
TWILIO_CLIENT = None
if TWILIO_SID and TWILIO_AUTH:
    try:
        TWILIO_CLIENT = Client(TWILIO_SID, TWILIO_AUTH)
        print("✅ Twilio client initialized.")
    except Exception as e:
        print(f"❌ Failed to initialize Twilio Client: {e}")
        TWILIO_CLIENT = None
else:
    print("⚠️ Twilio credentials missing. Twilio features disabled.")

# ---------------- Collections ----------------

COL_DOCTORS = mongo_db["doctors"] if mongo_db is not None else None
COL_APPOINTMENTS = mongo_db["appointments"] if mongo_db is not None else None
COL_CALL_LOGS = mongo_db["call_logs"] if mongo_db is not None else None
COL_SESSIONS = mongo_db["session_history"] if mongo_db is not None else None
COL_EMBEDDINGS = mongo_db["embeddings"] if mongo_db is not None else None
COL_AUDIO_METADATA = mongo_db["audio_metadata"] if mongo_db is not None else None
COL_AUDIO = COL_AUDIO_METADATA

# ---------------- Ensure Indexes ----------------
def ensure_indexes():
    try:
        if COL_DOCTORS is not None:
            COL_DOCTORS.create_index([("name", ASCENDING)], background=True)
            COL_DOCTORS.create_index([("specialty", ASCENDING)], background=True)
        if COL_APPOINTMENTS is not None:
            COL_APPOINTMENTS.create_index([("patient_id", ASCENDING)], background=True)
            COL_APPOINTMENTS.create_index([("date", ASCENDING), ("time", ASCENDING)], background=True)
            
        if COL_CALL_LOGS is not None:
            COL_CALL_LOGS.create_index([("call_sid", ASCENDING)], background=True)
            COL_CALL_LOGS.create_index([("phone", ASCENDING)], background=True) 
            
        if COL_SESSIONS is not None:
            # The session_id index is causing E11000, but we keep it here as a dev decision;
            # the fix is applied in the voice() function.
            COL_SESSIONS.create_index([("phone", ASCENDING)], background=True) 
            COL_SESSIONS.create_index([("session_id", ASCENDING)], unique=True, background=True) 
            
        if COL_EMBEDDINGS is not None:
            COL_EMBEDDINGS.create_index([("source_path", ASCENDING), ("rec_id", ASCENDING)], unique=True, background=True)
        if COL_AUDIO_METADATA is not None:
            COL_AUDIO_METADATA.create_index([("session_id", ASCENDING)], background=True)
            
        print("✅ MongoDB indexes ensured")
    except Exception as e:
        print("⚠️ Could not create MongoDB indexes:", e)

# ---------------- Create Dummy Doctors if Empty ----------------
def create_dummy_doctors():
    if COL_DOCTORS is not None:
        try:
            if COL_DOCTORS.count_documents({}) == 0:
                print("⚠️ Creating DUMMY doctors collection for demonstration.")
                dummy_docs = [
                    {"name": "Priya Sharma", "specialty": "Dermatologist", "fees": 1200, "availability": "Mon-Fri 9AM-5PM"},
                    {"name": "Rajiv Menon", "specialty": "Dermatologist", "fees": 1000, "availability": "Tues, Thurs 2PM-8PM"},
                    {"name": "Shaam Rao", "specialty": "General Physician", "fees": 800, "availability": "Mon-Sat 10AM-6PM"},
                    {"name": "Kiran Varma", "specialty": "Cardiologist", "fees": 1500, "availability": "Wed 1PM-4PM"}
                ]
                COL_DOCTORS.insert_many(dummy_docs)
            else:
                print("doctors collection exists")
        except Exception as e:
            print("❌ Error creating dummy doctors:", e)

# ---------open API configuration-----------------

try:
    OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    OPENAI_CLIENT = None

def save_json_local(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("❌ save_json_local error:", e)

def load_json_local(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("❌ load_json_local error:", e)
        return []


if COL_DOCTORS is None and not os.path.exists(DOCTOR_FILE):
    demo_docs = [
        {"name": "Priya Sharma", "specialty": "Dermatologist", "fees": 1200, "availability": "Mon-Fri 9AM-5PM"},
        {"name": "Rajiv Menon", "specialty": "Dermatologist", "fees": 1000, "availability": "Tues, Thurs 2PM-8PM"},
        {"name": "Shaam Rao", "specialty": "General Physician", "fees": 800, "availability": "Mon-Sat 10AM-6PM"},
        {"name": "Kiran Varma", "specialty": "Cardiologist", "fees": 1500, "availability": "Wed 1PM-4PM"}
    ]
    save_json_local(DOCTOR_FILE, demo_docs)

# ---------------- Globals and locks ----------------


NEURAL_VOICE = 'Google.en-IN-Wavenet-E'

PERSONALITY = {
    "tone": "friendly",
    "greeting": "Welcome to Patient Services Hospital. How can I assist you today?", 
    "outbound_greeting": "Hello, this is Clara from Patient Services Hospital calling from patient services. How can I help you today?", 
    "fallback": "I'm sorry, I didn't quite catch that. Could you please say that again?",
    "farewell": "Thank you for giving your precious time in Patient Services Hospital. Have a nice day! Goodbye."
}

CLINIC_INFO = {
    "emergency_number": "999 or 911", 
    "operating_hours": "Monday to Saturday, from 9 AM to 6 PM.",
    "address": "456 Health St, Pune, Maharashtra, India.",
    "phone_number": "020-1234-5678",
    "insurance_accepted": "We accept most major insurance providers and card payments. Please check with your specific provider upon arrival.",
    "cancellation_policy": "There is no cancellation fee if you cancel at least 2 hours before your appointment. Full fee may apply for late cancellations.",
    "late_policy": "We allow a 15-minute grace period. If you arrive later, your appointment may need to be rescheduled.",
    "typical_appointment_length": "A typical first-time appointment lasts 30 minutes.",
    "documents_to_bring": "Please bring your Aadhar Card or PAN Card for identity verification."
}

THINKING_PHRASES = [
    "Just a moment while I check the schedule.",
    "Hold on a sec, I'm pulling up the details now.",
    "Give me a moment to confirm that.",
    "Ah, let me quickly check the system for you.",
    "Let me see what's available.",
    "One moment, I'll confirm that information for you.",
    "Hold on a second, I will check and let you know."
]

def get_thinking_phrase():
    return random.choice(THINKING_PHRASES).strip('.')


app = Flask(__name__, static_url_path='/static')

# ---------------- In-Memory Session Stores ----------------
SESSIONS = {}
conversation_states = SESSIONS
AUDIO_STORE = {}

APPOINTMENT_LOCK = threading.Lock()
CALL_LOG_LOCK = threading.Lock()
DOCTOR_LOCK = threading.Lock()
SESSION_HISTORY_LOCK = threading.Lock()
RAG_LOCK = threading.Lock()
DOCTOR_DATA = []


# ---------------- Mongo-Mapped File -> Collection Utility ----------------
def _collection_for_path(path):
    """Map old JSON file paths to MongoDB collections."""
    if path == APPOINTMENT_FILE:
        return COL_APPOINTMENTS
    if path == DOCTOR_FILE:
        return COL_DOCTORS
    if path == CALL_LOG_FILE:
        return COL_CALL_LOGS
    if path == SESSION_HISTORY_FILE:
        return COL_SESSIONS
    if path == AUDIO_METADATA_FILE:
        return COL_AUDIO_METADATA 

    return None

# ---------------- JSON → Mongo Loader ----------------
def load_json(path):
    col = _collection_for_path(path)
    if col is None:
      
        return load_json_local(path)

    try:
        return list(col.find({}, {"_id": 0}))
    except Exception as e:
        print(f"⚠️ MongoDB load error for {path}:", e)
        return []

# ---------------- JSON → Mongo Saver ----------------
def save_json(path, data):
    col = _collection_for_path(path)
    if col is None:
       
        save_json_local(path, data)
        return

    try:
        col.delete_many({})

        if isinstance(data, list):
            docs = []
            for d in data:
                if "_id" in d:
                    d = {k: v for k, v in d.items() if k != "_id"}
                docs.append(d)
            if docs:
                col.insert_many(docs)

        elif isinstance(data, dict):
            d = {k: v for k, v in data.items() if k != "_id"}
            col.insert_one(d)

        else:
            col.insert_one({"content": data})

    except Exception as e:
        print(f"❌ MongoDB save error for {path}", e)

# ---------------- Session history helpers...
def load_session_history():
    """Load all session histories from MongoDB or local file."""
    col = _collection_for_path(SESSION_HISTORY_FILE)
    SESSIONS.clear()
    
    if col is not None:
        try:
            db_sessions = list(col.find({}, {"_id": 0}))
        except Exception as e:
            print("❌ Failed to load sessions from MongoDB:", e)
            db_sessions = []
            
        loaded_count = 0
        for s in db_sessions:
            phone = s.pop("phone", None)
            if phone:
                SESSIONS[phone] = {
                    "history": s.get("history", []),
                    "timestamp": s.get("timestamp")
                }
                loaded_count += 1
        print(f"Loaded {loaded_count} MongoDB session histories.")
    else:
        histories = load_json_local(SESSION_HISTORY_FILE)
        loaded_count = 0
        for s in histories:
            phone = s.pop("phone", None)
            if phone:
                SESSIONS[phone] = {
                    "history": s.get("history", []),
                    "timestamp": s.get("timestamp")
                }
                loaded_count += 1
        print(f"Loaded {loaded_count} local session histories.")


def persist_session_for_phone(phone: str, data: Dict[str, Any]):
    """Saves the current session state for a given phone number."""
    if not phone:
        print("❌ Cannot persist session: phone number is missing.")
        return

    col = _collection_for_path(SESSION_HISTORY_FILE)

    data_to_set = {
        "phone": phone,
        "history": data.get("history", []),
        "timestamp": datetime.datetime.now(pytz.utc).isoformat()
    }
   
    SESSIONS[phone] = data_to_set.copy()
    
    with SESSION_HISTORY_LOCK:
        if col is not None:
           
            col.update_one(
                {"phone": phone},
                {"$set": data_to_set},
                upsert=True
            )
        else:
           
            histories = load_json_local(SESSION_HISTORY_FILE) or []
            updated = False
            s = data_to_set.copy() 
            for i, existing_s in enumerate(histories):
                if existing_s.get("phone") == phone:
                    histories[i] = s
                    updated = True
                    break
            if not updated:
                histories.append(s)
            save_json_local(SESSION_HISTORY_FILE, histories)

def log_session_interaction(phone: str, call_sid: str, user_text: str, ai_reply: str, step: str):
    """Logs a single turn of conversation to call logs (COL_CALL_LOGS is now for turn-logging)."""
    log_entry = {
        "timestamp": datetime.datetime.now(pytz.utc).isoformat(),
        "phone": phone,
        "call_sid": call_sid,
        "user_text": user_text,
        "ai_reply": ai_reply,
        "step": step 
    }
    
    col = _collection_for_path(CALL_LOG_FILE)
    if col is not None:
        try:
            col.insert_one(log_entry)
        except Exception as e:
            print(f"❌ MongoDB call log insert error: {e}")
    else:
        with CALL_LOG_LOCK:
            logs = load_json_local(CALL_LOG_FILE)
            logs.append(log_entry)
            save_json_local(CALL_LOG_FILE, logs)


# ---------------- Doctor data management ----------------
def load_doctors():
    """Load doctor data from MongoDB or local file."""
    global DOCTOR_DATA
    with DOCTOR_LOCK:
        DOCTOR_DATA = load_json(DOCTOR_FILE)
        if not DOCTOR_DATA and COL_DOCTORS is not None:
            create_dummy_doctors()
            DOCTOR_DATA = load_json(DOCTOR_FILE)
        return DOCTOR_DATA

def get_doctor_info_string():
    """Formats doctor data for use in the LLM system prompt."""
    load_doctors()
    if not DOCTOR_DATA:
        return "No doctor data available."

    info_list = []
    for doc in DOCTOR_DATA:
        info_list.append(
            f"Dr. {doc.get('name', 'N/A')} ({doc.get('specialty', 'N/A')}), "
            f"Fees: {doc.get('fees', 'N/A')} rupees, "
            f"Availability: {doc.get('availability', 'N/A')}."
        )
    return "\n".join(info_list)

def find_doctor_by_specialty(target_specialty: str, limit: int = 2) -> List[Dict[str, Any]]:
    """Finds available doctors by matching specialty, or falls back to General Physician."""
    load_doctors()
    matched_doctors = []
    for doc in DOCTOR_DATA:
        spec = (doc.get("specialty") or "").lower()
        if target_specialty.lower() in spec:
            matched_doctors.append(doc)

    if not matched_doctors and target_specialty != "General Physician":
        for doc in DOCTOR_DATA:
            spec = (doc.get("specialty") or "").lower()
            if "general physician" in spec:
                matched_doctors.append(doc)

    if not matched_doctors:
        matched_doctors = DOCTOR_DATA
        
    current_day_abbr = datetime.datetime.now().strftime("%a") 
    filtered_doctors = []
    for doc in matched_doctors:
        availability = doc.get("availability", "").split()
        if current_day_abbr in availability[0] or "Any" in doc.get("availability", ""):
            filtered_doctors.append(doc)
    
    if not filtered_doctors:
        filtered_doctors = matched_doctors

    return filtered_doctors[:limit]

def find_doctor_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Find a doctor by name (partial or full match)."""
    name_low = name.lower().strip().replace('dr.', '').strip()
    load_doctors()
    for doc in DOCTOR_DATA:
        doc_name_low = doc.get("name", "").lower().strip().replace('dr.', '').strip()
        if name_low in doc_name_low or doc_name_low in name_low:
            return doc
    return None

# ---------------- Appointment data management ----------------

def load_appointments():
    """Load appointment data from MongoDB or local file."""
    with APPOINTMENT_LOCK:
        return load_json(APPOINTMENT_FILE)

def patient_id_generator(patient_name):
    """Simple ID generator for demo purposes."""
    return hashlib.sha256(patient_name.encode('utf-8')).hexdigest()[:10]

def create_appointment(patient: str, age: str, doctor: str, date_str: str, time_str: str, phone: str, problem: str) -> str:
    """Creates a new appointment and returns a confirmation message."""
    appointments = load_appointments()
    doctor_info = find_doctor_by_name(doctor)
    doctor_fee = doctor_info.get('fees') if doctor_info else "N/A"

    # CRITICAL: Basic conflict check (for demo - checks for exact patient/time slot)
    for appt in appointments:
        if appt.get("doctor") == doctor and appt.get("date") == date_str and appt.get("time") == time_str:
             return f"❌ Conflict: Dr. {doctor} is already booked at {time_str} on {date_str}. Please try another time."

    new_appointment = {
        "patient_id": patient_id_generator(patient),
        "patient": patient,
        "age": age,
        "doctor": doctor,
        "date": date_str,
        "time": time_str,
        "phone": phone,
        "problem": problem,
        "fee": doctor_fee,
        "timestamp": datetime.datetime.now(pytz.utc).isoformat()
    }

    with APPOINTMENT_LOCK:
        appointments.append(new_appointment)
        save_json(APPOINTMENT_FILE, appointments)

    try:
        dt_obj = datetime.datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M")
        local_tz = pytz.timezone('Asia/Kolkata')
        dt_local = pytz.utc.localize(dt_obj).astimezone(local_tz)
        time_display = dt_local.strftime('%I:%M %p on %A, %B %d')
    except Exception:
        time_display = f"{time_str} on {date_str}"


    return (
        f"✅ All set! Your appointment with Dr. {doctor} has been successfully booked for {time_display}. "
        f"The checkup fee is {doctor_fee} rupees. Please don't forget to bring an Aadhar Card or PAN Card for patient identity verification."
    )

def find_latest_appointment(patient_name: str, phone: str) -> Optional[Dict[str, Any]]:
    """Finds the latest appointment for a given patient name or phone number."""
    appointments = load_appointments()
    patient_name_low = patient_name.lower().strip()
    
    matching_appts = [
        appt for appt in appointments
        if (patient_name_low in appt.get("patient", "").lower() or 
            appt.get("phone") == phone)
    ]
    
    if not matching_appts:
        return None

    matching_appts.sort(key=lambda x: x.get("timestamp"), reverse=True)
    return matching_appts[0]

def update_appointment(patient: str, doctor: str, old_date: str, old_time: str, new_date: str, new_time: str, new_phone: str = None) -> str:
    """Updates an existing appointment."""
    appointments = load_appointments()
    patient_low = patient.lower().strip()
    doctor_low = doctor.lower().strip().strip('.')

    appt_index = -1
    for i, appt in enumerate(appointments):
        appt_patient_low = appt.get("patient", "").lower().strip()
        appt_doctor_low = appt.get("doctor", "").lower().strip().strip('.')

        if (patient_low in appt_patient_low or appt_patient_low in patient_low) and \
           (doctor_low in appt_doctor_low or appt_doctor_low in doctor_low):
            
            if appt.get("date") == old_date and appt.get("time") == old_time:
                appt_index = i
                break
            
            if appt_index == -1: 
                appt_index = i
                
    if appt_index == -1:
        return f"❌ Sorry, I couldn't find an appointment for {patient} with Dr. {doctor} that matches your request. Please try confirming the original date or doctor's name."

   
    for i, appt in enumerate(appointments):
        if i != appt_index: 
             if appt.get("doctor") == doctor and appt.get("date") == new_date and appt.get("time") == new_time:
                return f"❌ Conflict: Dr. {doctor} is already booked at {new_time} on {new_date}. Please try another time for rescheduling."

   
    original_appt = appointments[appt_index]
    
    original_date_str = original_appt['date']
    original_time_str = original_appt['time']
    original_doctor = original_appt['doctor']
    
    appointments[appt_index]["date"] = new_date
    appointments[appt_index]["time"] = new_time
    if new_phone:
        appointments[appt_index]["phone"] = new_phone
    appointments[appt_index]["timestamp"] = datetime.datetime.now(pytz.utc).isoformat()


    with APPOINTMENT_LOCK:
        save_json(APPOINTMENT_FILE, appointments)

    try:
        dt_obj = datetime.datetime.strptime(f"{new_date} {new_time}", "%d-%m-%Y %H:%M")
        local_tz = pytz.timezone('Asia/Kolkata')
        dt_local = pytz.utc.localize(dt_obj).astimezone(local_tz)
        time_display = dt_local.strftime('%I:%M %p on %A, %B %d')
    except Exception:
        time_display = f"{new_time} on {new_date}"
    
    return (
        f"✅ Your appointment for {patient} with Dr. {original_doctor} has been successfully rescheduled "
        f"from {original_time_str} on {original_date_str} to the new time of {time_display}."
    )

def cancel_appointment(patient: str, doctor: str) -> str:
    """Cancels an existing appointment."""
    appointments = load_appointments()
    patient_low = patient.lower().strip()
    doctor_low = doctor.lower().strip().strip('.')

    appt_index = -1
    for i, appt in enumerate(appointments):
        appt_patient_low = appt.get("patient", "").lower().strip()
        appt_doctor_low = appt.get("doctor", "").lower().strip().strip('.')
        if (patient_low in appt_patient_low or appt_patient_low in patient_low) and \
           (doctor_low in appt_doctor_low or appt_doctor_low in doctor_low):
            appt_index = i
            break

    if appt_index == -1:
        return f"❌ Sorry, I couldn't find an appointment for {patient} with Dr. {doctor} to cancel. Can you confirm the full name?"

    
    canceled_appt = appointments.pop(appt_index)

    with APPOINTMENT_LOCK:
        save_json(APPOINTMENT_FILE, appointments)

    try:
        date_str = canceled_appt['date']
        time_str = canceled_appt['time']
        dt_obj = datetime.datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M")
        local_tz = pytz.timezone('Asia/Kolkata')
        dt_local = pytz.utc.localize(dt_obj).astimezone(local_tz)
        time_display = dt_local.strftime('%I:%M %p on %A, %B %d')
    except Exception:
        time_display = f"{canceled_appt.get('time', 'N/A')} on {canceled_appt.get('date', 'N/A')}"
        
    return (
        f"Okay, the appointment for {patient} with Dr. {doctor or canceled_appt['doctor']} "
        f"on {time_display} has been successfully cancelled. Is there anything else I can help you with today?"
    )

# ---------------- Utility functions ----------------

def parse_date_time(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Tries to parse date and time using dateparser, returns (DD-MM-YYYY, HH:MM)."""
    
    settings = {'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'Asia/Kolkata', 'RETURN_AS_TIMEZONE_AWARE': True}
    
   
    parsed_dt = dateparser.parse(text, settings=settings)
    
    date_str = None
    time_str = None

    if parsed_dt:
        
        local_tz = pytz.timezone('Asia/Kolkata')
        if parsed_dt.tzinfo is None:
           
            parsed_dt = local_tz.localize(parsed_dt)
        else:
            parsed_dt = parsed_dt.astimezone(local_tz)
            
        date_str = parsed_dt.strftime("%d-%m-%Y")
        time_str = parsed_dt.strftime("%H:%M")

    return date_str, time_str

def extract_slots_from_text(text: str) -> Dict[str, Any]:
    """Uses basic regex and dateparser to extract key information from a sentence."""
    text_low = text.lower()
    slots = {}

    for doc in DOCTOR_DATA:
        doc_name_low = doc['name'].lower()
        if doc_name_low in text_low or f"dr. {doc_name_low}" in text_low:
            slots["doctor"] = doc['name']
            break

    age_match = re.search(r'\b(age|is)\s+(\d{1,3})\s*(year|yr|y\.o|years|yrs)', text_low)
    if age_match:
        slots["age"] = age_match.group(2)
    single_num_match = re.search(r'\b(\d{1,3})\s*$', text_low)
    if not age_match and single_num_match and 'age' in text_low:
        slots["age"] = single_num_match.group(1)

    phone_match = re.search(r'(\d{10})', text_low)
    if phone_match:
        phone = phone_match.group(1)
        
        if phone.startswith('0'):
            slots["phone"] = "+91" + phone[1:]
        else:
            slots["phone"] = "+91" + phone
            
    date_str, time_str = parse_date_time(text)
    if date_str:
        slots["date"] = date_str
    if time_str:
        slots["time"] = time_str
        
    return slots

def get_doctor_details_response(doctor_name: str) -> str:
    """Provides details about a specific doctor."""
    doctor = find_doctor_by_name(doctor_name)
    if doctor:
        return (
            f"Dr. {doctor['name']} is a {doctor['specialty']} with a fee of {doctor['fees']} rupees. "
            f"They are typically available {doctor['availability']}. Would you like to book an appointment now?"
        )
    return f"I couldn't find a doctor named {doctor_name}. Can you confirm the name or perhaps tell me the patient's concern?"

def get_clinic_info_response(user_text_low: str) -> Optional[str]:
    """Provides a fast, rule-based response for general clinic queries/interruptions."""
    
 
    if any(phrase in user_text_low for phrase in ["who are you", "who am i speaking to", "who is this", "your name"]):
        return "I am an AI Patient Helper Voice Agent for Patient Services Hospital, here to help you with appointments and general queries."
    
    if any(phrase in user_text_low for phrase in ["where is this located", "where are you", "location", "address", "hospital location"]):
        return f"Our clinic is located at {CLINIC_INFO['address']}. How can I assist you further?"
    
    if any(phrase in user_text_low for phrase in ["fees", "cost", "how much", "charge", "appointment fee"]):
       
        return "Doctor fees vary by specialty and doctor. For general appointments, fees start from 800 rupees. Is there a specific doctor you are asking about?"

   
    if any(phrase in user_text_low for phrase in ["emergency", "ambulance", "urgent"]):
        return f"For medical emergencies, please call {CLINIC_INFO['emergency_number']} immediately. How can I assist you with your appointment today?"
    
    if any(phrase in user_text_low for phrase in ["hours", "open", "close", "time"]):
        return f"We are open {CLINIC_INFO['operating_hours']}. How can I assist you further?"
    
    if any(phrase in user_text_low for phrase in ["insurance", "payment", "card"]):
        return f"{CLINIC_INFO['insurance_accepted']} How can I assist you with your appointment today?"
        
    if any(phrase in user_text_low for phrase in ["cancellation policy", "cancel fee", "late"]):
        return f"Our cancellation policy is: {CLINIC_INFO['cancellation_policy']} and {CLINIC_INFO['late_policy']}. How can I assist you with your appointment?"

    if any(phrase in user_text_low for phrase in ["bring", "documents", "id card", "adhar", "pan card"]):
        return f"{CLINIC_INFO['documents_to_bring']} How can I assist you with your appointment today?"

    return None

# ---------------- LLM Interaction ----------------

SYSTEM_PROMPT = f"""
YOU ARE: A **lightning-fast, responsive, human-like** AI Patient Helper Voice Agent for a hospital/clinic.

You must behave like a professional, empathetic, and knowledgeable medical support assistant.

🎯 CORE OBJECTIVES
1. **Ultra-fast response generation: MINIMIZE LATENCY (Max 1-2 short sentences).**
2. **Real-time interruption handling (Barge-in):** Answer the interruption **DIRECTLY and CONCISELY** first, then smoothly ask the next question required for the current transaction.
3. **CRITICAL TRANSACTION FLOW ENFORCEMENT (Symptom-First Booking):**
    For 'book_appointment', you **MUST** follow this sequence:
    1. **Symptom/Problem** (What is the issue?) ->
    2. **Suggest Specialist** (Which specialist is required for the symptom?) ->
    3. **Suggest Doctor/Availability** (Suggest 1-2 available doctors/times for that specialty from the DOCTOR LIST.)
    You must extract the symptom first and use it to guide the selection of the correct specialist/doctor.
4. Provide accurate answers using the **DOCTOR LIST** and **CLINIC INFO**.
5. Maintain natural, professional conversation flow.

🧠 CRITICAL INTERRUPTION RULE (MUST FOLLOW): 
If the user interrupts or asks a question unrelated to the current transaction step (e.g., asks for fees mid-booking, or "Who are you?"), you **MUST** answer the interruption **FIRST** (Max 1 sentence), and then immediately return to the transaction flow by asking the single next required question.

⚙️ RESPONSE STYLE REQUIREMENTS
- Speak **fast, concise, clear sentences**. Max 2 sentences unless confirming details.
- **Do not use robotic pauses.**
- Always confirm user intent before finalizing appointments.
- **NEVER LEAVE SILENCE GAPS; maintain continuous conversational flow.**

🩺 DOMAIN EXPERTISE & DATA
- Clinic Location: {CLINIC_INFO['address']}
- Clinic Phone: {CLINIC_INFO['phone_number']}
- Clinic Hours: {CLINIC_INFO['operating_hours']}
- Emergency Number: {CLINIC_INFO['emergency_number']}
- DOCTOR LIST (Use this for availability and fees):
{get_doctor_info_string()}

TRANSACTIONAL STATE (For context, but prioritize user's NEW intent if detected):
- Current Step: {{step}}
- Awaiting Slot: {{awaiting}}
- Current Slots: {{temp_slots}}

Based on the user's input, your current state, and the DOCTOR LIST, your task is to either:
1. Provide a direct, concise answer to an interruption/general query.
2. Ask the next, *single* most relevant question to gather the missing slot for the current step.
3. Output one of the specific ACTION keywords below (in JSON format) if the transaction is complete or a major state change is needed.

ACTION KEYWORDS: ['book_appointment', 'reschedule_appointment', 'cancel_appointment', 'check_appointment', 'farewell']
If the user's input strongly suggests a new flow (e.g., they are mid-booking but say "I want to check my appointment"), output the new action and provide a transition sentence.
"""


def call_llm_for_response(user_text: str, session_data: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """
    Primary function to communicate with the LLM when rule-based logic is insufficient.
    Returns: (ai_reply_text, next_action, updated_slots)
    """
    if not OPENAI_CLIENT:
        return PERSONALITY["fallback"], "clarify", {}
    
    history = session_data.get("history", [])
    temp_slots = session_data.get("temp_slots", {})
    step = session_data.get("step", "root")
    awaiting = session_data.get("awaiting")

    
    current_system_prompt = SYSTEM_PROMPT.replace("{{step}}", step).replace("{{awaiting}}", str(awaiting)).replace("{{temp_slots}}", json.dumps(temp_slots))

    messages = [{"role": "system", "content": current_system_prompt}]
    
    
    for turn in history[-6:]:
        if turn.get("user_text"):
            messages.append({"role": "user", "content": turn["user_text"]})
        if turn.get("ai_reply"):
            
            clean_reply = turn["ai_reply"].replace('**', '').strip()
            messages.append({"role": "assistant", "content": clean_reply})

   
    messages.append({"role": "user", "content": user_text})

    try:
        completion = OPENAI_CLIENT.chat.completions.create(
            
            model="gpt-3.5-turbo-0125", 
            messages=messages,
            temperature=0.7,
            max_tokens=60, 
            stop=["\n\n", "}", "..."]
        )
        
        raw_llm_output = completion.choices[0].message.content.strip()

    except Exception as e:
        print(f"❌ OpenAI API Error: {e}")
        return PERSONALITY["fallback"], "clarify", {}

    ai_reply = raw_llm_output
    action = "clarify" 
    llm_slots = {}
    
    match = re.search(r'\{[^{}]*\}', raw_llm_output)
    if match:
        try:
            json_str = match.group(0)
            llm_data = json.loads(json_str)
            
            action_key = llm_data.pop("ACTION", llm_data.pop("action", None))
            if action_key and action_key.lower() in ['book_appointment', 'reschedule_appointment', 'cancel_appointment', 'check_appointment', 'farewell']:
                action = action_key.lower()

            ai_reply_parts = raw_llm_output.split(json_str, 1)
            if ai_reply_parts[0].strip():
                ai_reply = ai_reply_parts[0].strip()
            
            llm_slots = {k: v for k, v in llm_data.items() if v is not None}

        except json.JSONDecodeError as e:
            print(f"⚠️ JSON Decode Error in LLM response: {e}")
            pass 

    
    if not ai_reply:
        if action == "farewell":
            ai_reply = PERSONALITY["farewell"]
        elif action != "clarify":
            
            ai_reply = "Understood. Let me initiate that for you."
        else:
            ai_reply = PERSONALITY["fallback"]

    extracted_slots = extract_slots_from_text(user_text)
    if extracted_slots:
        llm_slots.update(extracted_slots)

    return ai_reply, action, llm_slots

# ---------------- Transactional Flow Logic ----------------

def book_appointment_flow(user_text: str, session_data: Dict[str, Any], temp_slots: Dict[str, Any]) -> Tuple[str, str]:
    """Handles the state machine for booking an appointment."""
    patient = temp_slots.get("patient_name")
    age = temp_slots.get("age")
    problem = temp_slots.get("problem")
    doctor_name = temp_slots.get("doctor")
    date = temp_slots.get("date")
    time = temp_slots.get("time")
    phone_confirmed = temp_slots.get("phone")
    calling_number = session_data.get("from_number")

    session_data["step"] = "book_appointment"
    
    # 1. Gather Patient Name
    if not patient:
        session_data["awaiting"] = "patient_name"
        ai_reply = "I can book an appointment. What is the full name of the patient?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    # 2. Gather Problem/Doctor
    if not problem and not doctor_name:
        session_data["awaiting"] = "problem_or_doctor"
        ai_reply = f"Thank you, {patient}. What is the patient's main concern or which doctor would you like to see?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    # 3. Handle Problem/Suggest Doctor
    if problem and not doctor_name:
        
        suggested_specialty = "General Physician" 
        if "skin" in problem.lower() or "rash" in problem.lower() or "dermatology" in problem.lower():
            suggested_specialty = "Dermatologist"
        elif "heart" in problem.lower() or "cardiac" in problem.lower():
             suggested_specialty = "Cardiologist"
            
        available_doctors = find_doctor_by_specialty(suggested_specialty, limit=2)
        
        if available_doctors:
            doc_options = ", ".join([f"Dr. {doc['name']} ({doc['specialty']})" for doc in available_doctors])
            ai_reply = (
                f"Based on a {problem}, you need a {suggested_specialty}. "
                f"We have {doc_options} available today. Which doctor and what time would you prefer?"
            )
        else:
            ai_reply = f"We recommend a {suggested_specialty}. Which doctor and time would you like to book?"
        
        session_data["awaiting"] = "doctor_name"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"
    
    # 4. Confirm Doctor (if only doctor name but no time)
    if doctor_name and not date and not time:
        doctor_info = find_doctor_by_name(doctor_name)
        if not doctor_info:
            ai_reply = f"I couldn't find Dr. {doctor_name}. Can you confirm the doctor's name?"
            session_data["temp_slots"]["doctor"] = None # Clear bad slot
            session_data["awaiting"] = "doctor_name"
            session_data["last_ai_reply"] = ai_reply
            return ai_reply, "clarify"
        
        
        session_data["awaiting"] = "date_time"
        ai_reply = f"Thank you. Dr. {doctor_name} is available {doctor_info['availability']}. What date and time would you like to book?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    # 5. Gather Date/Time
    if doctor_name and not date and not time:
        
        session_data["awaiting"] = "date_time"
        ai_reply = f"I still need a valid date and time for the appointment with Dr. {doctor_name}. When would you like it?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    # 6. Gather Age and Phone Number
    if not age:
        session_data["awaiting"] = "age"
        ai_reply = f"Thank you. Can you please confirm the patient's age?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"
    
    if not phone_confirmed:
        session_data["awaiting"] = "phone"
    
        if calling_number and is_valid_e164(calling_number):
            display_num = f"{calling_number[-4:]}"
            ai_reply = f"Should I register your appointment with this number ending in {display_num}, or another number?"
        else:
            ai_reply = "What is the best 10-digit contact number for this appointment?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    # 7. Final Confirmation (All slots filled)
    session_data["awaiting"] = "confirm_booking"
    
    doctor_info = find_doctor_by_name(doctor_name)
    fee = doctor_info.get('fees', 'N/A') if doctor_info else 'N/A'
    
    try:
        dt_obj = datetime.datetime.strptime(f"{date} {time}", "%d-%m-%Y %H:%M")
        local_tz = pytz.timezone('Asia/Kolkata')
        dt_local = pytz.utc.localize(dt_obj).astimezone(local_tz)
        time_display = dt_local.strftime('%I:%M %p on %A, %B %d')
    except Exception:
        time_display = f"{time} on {date}"

    ai_reply = (
        f"Excellent. I have all the details now. Dr. {doctor_name}'s checkup fees will be {fee} rupees. "
        f"Just to confirm: Your appointment for {patient}, age {age}, is booked for {time_display}. "
        f"Is this correct? Say 'yes' or 'no'."
    )
    session_data["last_ai_reply"] = ai_reply
    return ai_reply, "clarify"


def check_appointment_flow(user_text: str, session_data: Dict[str, Any], temp_slots: Dict[str, Any]) -> Tuple[str, str]:
    """Handles the state machine for checking an appointment."""
    patient = temp_slots.get("patient_name")
    phone = session_data.get("from_number")

    session_data["step"] = "check_appointment"

    if not patient:
        session_data["awaiting"] = "patient_name"
        ai_reply = "I can check your appointment. What is the full name of the patient?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    thinking_phrase = get_thinking_phrase()

    latest_appt = find_latest_appointment(patient, phone)

    if latest_appt:
        temp_slots["doctor"] = latest_appt["doctor"]
        temp_slots["date"] = latest_appt["date"]
        temp_slots["time"] = latest_appt["time"]
        

        try:
            dt_obj = datetime.datetime.strptime(f"{latest_appt['date']} {latest_appt['time']}", "%d-%m-%Y %H:%M")
            local_tz = pytz.timezone('Asia/Kolkata')
            dt_local = pytz.utc.localize(dt_obj).astimezone(local_tz)
            time_display = dt_local.strftime('%I:%M %p on %A, %B %d')
        except Exception:
            time_display = f"{latest_appt.get('time', 'N/A')} on {latest_appt.get('date', 'N/A')}"

        ai_reply = (
            f"**{thinking_phrase}.** I have found an appointment for {patient} with Dr. {latest_appt['doctor']} "
            f"on {time_display}. Is there anything else I can assist you with?"
        )
        
    else:
        ai_reply = f"**{thinking_phrase}.** I couldn't find an active appointment for {patient} registered with this number. Would you like to book a new one?"
        
    session_data["step"] = "root" 
    session_data["awaiting"] = None
    session_data["temp_slots"] = {}
    session_data["last_ai_reply"] = ai_reply
    return ai_reply, "clarify"


def cancel_appointment_flow(user_text: str, session_data: Dict[str, Any], temp_slots: Dict[str, Any]) -> Tuple[str, str]:
    """Handles the state machine for canceling an appointment."""
    patient = temp_slots.get("patient_name")
    doctor = temp_slots.get("doctor")
    phone = session_data.get("from_number")

    session_data["step"] = "cancel_appointment"

    if not patient:
        session_data["awaiting"] = "patient_name"
        ai_reply = "I can help you cancel. What is the full name of the patient?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    thinking_phrase = get_thinking_phrase()
    latest_appt = find_latest_appointment(patient, phone)

    if latest_appt:
        
        temp_slots["doctor"] = latest_appt["doctor"] 
        
        try:
            dt_obj = datetime.datetime.strptime(f"{latest_appt['date']} {latest_appt['time']}", "%d-%m-%Y %H:%M")
            local_tz = pytz.timezone('Asia/Kolkata')
            dt_local = pytz.utc.localize(dt_obj).astimezone(local_tz)
            time_display = dt_local.strftime('%I:%M %p on %A, %B %d')
        except Exception:
            time_display = f"{latest_appt.get('time', 'N/A')} on {latest_appt.get('date', 'N/A')}"

        ai_reply = (
            f"**{thinking_phrase}.** I have found an appointment for {patient} with Dr. {latest_appt['doctor']} on {time_display}. "
            f"Are you sure you want to cancel this appointment? Say 'yes' to confirm."
        )
        session_data["awaiting"] = "confirm_cancel"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"
    else:
        ai_reply = f"**{thinking_phrase}.** I couldn't find an active appointment for {patient} to cancel. Please confirm the patient's name and doctor."
        session_data["step"] = "root"
        session_data["awaiting"] = None
        session_data["temp_slots"] = {}
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"


def reschedule_appointment_flow(user_text: str, session_data: Dict[str, Any], temp_slots: Dict[str, Any]) -> Tuple[str, str]:
    """Handles the state machine for rescheduling an appointment."""
    patient = temp_slots.get("patient_name")
    doctor = temp_slots.get("doctor")
    new_date = temp_slots.get("date")
    new_time = temp_slots.get("time")
    phone = session_data.get("from_number")

    session_data["step"] = "reschedule_appointment"

    if not patient or not doctor:
        session_data["awaiting"] = "reschedule_details"
        ai_reply = "I can help you reschedule. For which patient and doctor is the appointment you want to change?"
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

    
    thinking_phrase = get_thinking_phrase()
    latest_appt = find_latest_appointment(patient, phone)
    
    if not latest_appt:
        ai_reply = f"**{thinking_phrase}.** I couldn't find an existing appointment for {patient} with Dr. {doctor}. Please confirm the patient's full name and doctor."
        session_data["step"] = "root"
        session_data["awaiting"] = None
        session_data["temp_slots"] = {}
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"
    
    
    temp_slots["old_date"] = latest_appt.get("date")
    temp_slots["old_time"] = latest_appt.get("time")
    
    
    if not new_date or not new_time:
        
        session_data["awaiting"] = "new_date_time"
        ai_reply = (
            f"**{thinking_phrase}.** I found your appointment with Dr. {latest_appt['doctor']} on {latest_appt['date']} at {latest_appt['time']}. "
            f"What is the new date and time you would like to move it to?"
        )
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"

   
    session_data["awaiting"] = "confirm_reschedule"
    
    try:
        dt_obj = datetime.datetime.strptime(f"{new_date} {new_time}", "%d-%m-%Y %H:%M")
        local_tz = pytz.timezone('Asia/Kolkata')
        dt_local = pytz.utc.localize(dt_obj).astimezone(local_tz)
        time_display = dt_local.strftime('%I:%M %p on %A, %B %d')
    except Exception:
        time_display = f"{new_time} on {new_date}"
        
    ai_reply = (
        f"Just to confirm, you want to reschedule {patient}'s appointment with Dr. {doctor} to {time_display}. "
        f"Is this correct? Say 'yes' or 'no'."
    )
    session_data["last_ai_reply"] = ai_reply
    return ai_reply, "clarify"


def handle_user_input(user_text: str, session_data: Dict[str, Any]) -> Tuple[str, str]:
    """Main state machine entry point."""
    step = session_data.get("step", "root")
    awaiting = session_data.get("awaiting")
    temp_slots = session_data.get("temp_slots", {})
    user_text_low = user_text.lower().strip()

    
    instant_reply = get_clinic_info_response(user_text_low)
    
    if instant_reply:
        
        if step != "root" and awaiting:
            
            
            original_step = step
            
            
            if awaiting in ["confirm_booking", "confirm_cancel", "confirm_reschedule"]:
                
                if original_step == "book_appointment":
                    resumption_reply, _ = book_appointment_flow(user_text, session_data, temp_slots)
                elif original_step == "reschedule_appointment":
                    resumption_reply, _ = reschedule_appointment_flow(user_text, session_data, temp_slots)
                elif original_step == "cancel_appointment":
                    resumption_reply, _ = cancel_appointment_flow(user_text, session_data, temp_slots)
                else:
                    resumption_reply = PERSONALITY["fallback"] 
                
                ai_reply = f"{instant_reply} Also, I apologize for the interruption, {resumption_reply}"
                
            else:
                 last_q = session_data.get("last_ai_reply", "What was the last thing I asked you about?")
                 
                 clean_last_q = last_q.replace('**', '').strip()
                 ai_reply = f"{instant_reply} Also, as I was saying— {clean_last_q}"
        else:
            
            ai_reply = instant_reply
            
        session_data["last_ai_reply"] = ai_reply
        session_data["history"].append({"user_text": user_text, "ai_reply": ai_reply})
        return ai_reply, "clarify" 

    
    intent = None
    if user_text_low in ["yes", "yeah", "yep", "correct", "confirm"]:
        intent = "yes"
    elif user_text_low in ["no", "nope", "incorrect", "wrong", "cancel"]:
        intent = "no"

    if awaiting == "confirm_booking" and intent == "yes":
        result_text = create_appointment(
            patient=temp_slots.get("patient_name"),
            age=temp_slots.get("age"),
            doctor=temp_slots.get("doctor"),
            date_str=temp_slots.get("date"),
            time_str=temp_slots.get("time"),
            phone=temp_slots.get("phone") or session_data.get("from_number"),
            problem=temp_slots.get("problem", "Appointment booking.")
        )
        session_data["step"] = "root"
        session_data["awaiting"] = None
        session_data["temp_slots"] = {}
        session_data["last_ai_reply"] = result_text
        return result_text, "clarify" 

    elif awaiting == "confirm_cancel" and intent == "yes":
        result_text = cancel_appointment(
            patient=temp_slots.get("patient_name"),
            doctor=temp_slots.get("doctor") 
        )
        session_data["step"] = "root"
        session_data["awaiting"] = None
        session_data["temp_slots"] = {}
        session_data["last_ai_reply"] = result_text
        return result_text, "clarify"

    elif awaiting == "confirm_reschedule" and intent == "yes":
        result_text = update_appointment(
            patient=temp_slots.get("patient_name"),
            doctor=temp_slots.get("doctor"),
            old_date=temp_slots.get("old_date"),
            old_time=temp_slots.get("old_time"),
            new_date=temp_slots.get("date"),
            new_time=temp_slots.get("time"),
            new_phone=temp_slots.get("phone")
        )
        session_data["step"] = "root"
        session_data["awaiting"] = None
        session_data["temp_slots"] = {}
        session_data["last_ai_reply"] = result_text
        return result_text, "clarify"

    
    elif awaiting in ["confirm_booking", "confirm_cancel", "confirm_reschedule"] and intent == "no":
        ai_reply = "Understood. The action has been stopped. Is there anything else I can help you with today?"
        session_data["step"] = "root"
        session_data["awaiting"] = None
        session_data["temp_slots"] = {}
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, "clarify"
    
    
    if step == "root" or step == "clarify":
        if any(w in user_text_low for w in ["check my appointment", "appointment status"]):
            return check_appointment_flow(user_text, session_data, temp_slots)
        if any(w in user_text_low for w in ["book appointment", "schedule appointment", "make an appointment"]):
            return book_appointment_flow(user_text, session_data, temp_slots)
        if any(w in user_text_low for w in ["cancel appointment", "call off"]):
            return cancel_appointment_flow(user_text, session_data, temp_slots)
        if any(w in user_text_low for w in ["reschedule appointment", "change appointment", "move my appointment"]):
            return reschedule_appointment_flow(user_text, session_data, temp_slots)


   
    ai_reply, action, llm_slots = call_llm_for_response(user_text, session_data)

    
    if llm_slots:
        temp_slots.update(llm_slots)
        session_data["temp_slots"] = temp_slots

    
    if action != "clarify" and action != step and action != "root" and action != "farewell":
        
        print(f"⚠️ Intent switch detected: {step} -> {action}")
        
        session_data["step"] = action
        session_data["awaiting"] = None
        
        if action == "book_appointment":
            return book_appointment_flow(user_text, session_data, temp_slots)
        elif action == "reschedule_appointment":
            return reschedule_appointment_flow(user_text, session_data, temp_slots)
        elif action == "cancel_appointment":
            return cancel_appointment_flow(user_text, session_data, temp_slots)
        elif action == "check_appointment":
            return check_appointment_flow(user_text, session_data, temp_slots)
        elif action == "farewell":
            return PERSONALITY["farewell"], "farewell"

    # --- Continue Current Flow ---
    if step == "book_appointment":
        return book_appointment_flow(user_text, session_data, temp_slots)
    elif step == "reschedule_appointment":
        return reschedule_appointment_flow(user_text, session_data, temp_slots)
    elif step == "cancel_appointment":
        return cancel_appointment_flow(user_text, session_data, temp_slots)
    elif step == "check_appointment":
        return check_appointment_flow(user_text, session_data, temp_slots)
    
    elif step == "root":
        if action == "farewell":
            return PERSONALITY["farewell"], "farewell"

        
        session_data["last_ai_reply"] = ai_reply
        return ai_reply, action 

    
    session_data["last_ai_reply"] = ai_reply
    return PERSONALITY["fallback"], "clarify" 

# ---------------- Twilio TwiML Generation ----------------

def tts_and_gather(text: str, session_data: Dict[str, Any], action_url: str) -> str:
    """Generates TwiML for speaking text and gathering the next input."""
    response = VoiceResponse()
    
   
    gather = response.gather(
        input='dtmf speech', 
        timeout=3, 
        action=action_url,
        num_digits=1, 
        speech_timeout='auto',
        speech_model='default',
        language='en-IN' 
    )
    
    
    clean_text = text.replace('**', '').strip() 
    gather.say(clean_text, voice=NEURAL_VOICE, language='en-IN') 
    

    if session_data.get("step") != "farewell":
        response.say(PERSONALITY["fallback"], voice=NEURAL_VOICE, language='en-IN')
        response.redirect(action_url)
    else:
        response.hangup()

    return str(response)

# ---------------- Twilio Webhook Endpoints ----------------

def load_session_from_db_or_create(from_number):
    """
    Loads session history (if available) but returns a FRESH transactional state 
    (step: root, awaiting: None, temp_slots: {}), fulfilling the 'FRESH START' requirement.
    """
    
    session = {
        "session_id": str(uuid.uuid4()),
        "step": "root", 
        "awaiting": None,
        "temp_slots": {},
        "last_ai_reply": "",
        "from_number": from_number,
        "history": [], 
    }

    
    existing_session = SESSIONS.get(from_number)
    if existing_session and existing_session.get("history"):
        session["history"] = existing_session["history"]
        print(f"Loaded session state for {from_number} from MongoDB.")
    
    
    session["history"] = session["history"][-20:]
    
    return session

def log_call(call_sid, phone, status, details=""):
    """Logs the call status (start, error, end) to call_logs collection. Using upsert for consistency."""
    col = COL_CALL_LOGS
    log_entry = {
        "timestamp": datetime.datetime.now(pytz.utc).isoformat(),
        "call_sid": call_sid,
        "phone": phone,
        "status": status,
        "details": details
    }
    
    if col is not None:
        try:
            
            col.insert_one(log_entry)
        except Exception as e:
            
            print(f"❌ MongoDB call log insert error: {e}")
    else:
        
        with CALL_LOG_LOCK:
            logs = load_json_local(CALL_LOG_FILE)
            logs.append(log_entry)
            save_json_local(CALL_LOG_FILE, logs)


@app.route("/voice", methods=["GET", "POST"])
def voice():
    """Twilio webhook for handling incoming and outgoing calls."""
    from_number = request.values.get("From")
    call_sid = request.values.get("CallSid")
    
    if not from_number:
        return str(VoiceResponse().say("I could not identify your phone number. Goodbye."))

    
    session_data = load_session_from_db_or_create(from_number)
    SESSIONS[from_number] = session_data

    
    ai_reply = PERSONALITY["greeting"]
    session_data["last_ai_reply"] = ai_reply
    
    
    log_call(call_sid, from_number, status="inbound_started")
    log_session_interaction(from_number, call_sid, "[INBOUND CALL START]", ai_reply, session_data.get("step"))
    
    
    persist_session_for_phone(from_number, session_data)

   
    return tts_and_gather(ai_reply, session_data, "/process")


@app.route("/process", methods=["GET", "POST"])
def process():
    """Twilio webhook for processing user speech input during a call."""
    from_number = request.values.get("From")
    call_sid = request.values.get("CallSid")
   
    user_text = request.values.get("SpeechResult", request.values.get("Digits", "")).strip()
    
    if not from_number or from_number not in SESSIONS:
        
        log_call(call_sid, from_number, status="error", details="Session not found.")
        return str(VoiceResponse().say(PERSONALITY["fallback"] + " Please try again."))
    
    session_data = SESSIONS[from_number]
    
    
    if not user_text:
        ai_reply = PERSONALITY["fallback"]
        action = session_data.get("step", "clarify")
    else:
       
        ai_reply, action = handle_user_input(user_text, session_data)

   
    print("-" * 50)
    print(f"User: {user_text} (Confidence: {request.values.get('Confidence', 'N/A')})")
    print(f"Session Step: {session_data.get('step')} | Awaiting: {session_data.get('awaiting')}")
    print(f"AI Reply: {ai_reply}")
    print(f"Next Action: {action}")
    print("-" * 50)

    
    session_data["history"].append({"user_text": user_text, "ai_reply": ai_reply})
    log_session_interaction(from_number, call_sid, user_text, ai_reply, session_data.get("step"))
    persist_session_for_phone(from_number, session_data)
    
    if action == "farewell":
        
        log_call(call_sid, from_number, status="completed", details="Call completed with farewell.")
        response = VoiceResponse()
        response.say(ai_reply, voice=NEURAL_VOICE, language='en-IN')
        response.hangup()
        return str(response)

    return tts_and_gather(ai_reply, session_data, "/process")


@app.route("/outbound_voice", methods=["GET", "POST"])
def outbound_voice():
    """Twilio webhook for handling the start of an outbound call."""
    from_number = request.values.get("To") 
    call_sid = request.values.get("CallSid")

    if not from_number:
        return str(VoiceResponse().say("I could not determine the target number. Goodbye."))

    
    session_data = load_session_from_db_or_create(from_number)
    SESSIONS[from_number] = session_data

   
    ai_reply = PERSONALITY["outbound_greeting"] 
    session_data["last_ai_reply"] = ai_reply

   
    log_call(call_sid, from_number, status="outbound_started")
    log_session_interaction(from_number, call_sid, "[OUTBOUND CALL START]", ai_reply, session_data.get("step"))
    
    
    persist_session_for_phone(from_number, session_data)

    
    return tts_and_gather(ai_reply, session_data, "/process")

# ---------------- Outbound Trigger & Ngrok ----------------


NGROK_TUNNEL = None
NGROK_LOCK = threading.Lock()

def start_ngrok(port, prefer_pyngrok=True):
    global PUBLIC_URL, NGROK_TUNNEL
    if PUBLIC_URL and PUBLIC_URL.startswith("http"):
        print("✅ PUBLIC_URL already set in .env. Skipping ngrok.")
        return

    if not NGROK_AUTHTOKEN:
        print("⚠️ NGROK_AUTHTOKEN not set. Cannot start ngrok.")
        return

    
    if prefer_pyngrok:
        try:
            from pyngrok import ngrok
            if NGROK_AUTHTOKEN:
                
                ngrok.set_auth_token(NGROK_AUTHTOKEN)
                tunnel = ngrok.connect(port, proto="http", bind_tls=True)
                
                with NGROK_LOCK:
                    NGROK_TUNNEL = tunnel
                    PUBLIC_URL = tunnel.public_url.replace("http://", "https://")
                    print(f"✅ Ngrok tunnel started. Public URL: {PUBLIC_URL}")
                    
            return

        except ImportError:
            print("⚠️ pyngrok not installed. Falling back to external ngrok or manual URL.")
        except Exception as e:
            
            print(f"❌ Ngrok connection failed: {e}")
            print("⚠️ Could not start ngrok. Please set PUBLIC_URL in .env or ensure ngrok is running and configured.")
            
    


def keep_ngrok_alive(port, interval=15):
    """Simple thread to periodically check and restart ngrok if needed."""
    global PUBLIC_URL, NGROK_TUNNEL
    while True:
        time.sleep(interval)
        if PUBLIC_URL and PUBLIC_URL.startswith("https://") and NGROK_TUNNEL:
            try:
                
                if str(NGROK_TUNNEL.public_url) not in PUBLIC_URL:
                    
                    with NGROK_LOCK:
                        PUBLIC_URL = str(NGROK_TUNNEL.public_url).replace("http://", "https://")
                        print(f"🔄 Ngrok URL refreshed: {PUBLIC_URL}")
                        
            except Exception as e:
                print(f"t={datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S%z')} lvl=warn msg=\"Stopping forwarder\" name=http-{port} acceptErr=\"failed to accept connection: Listener closed\"")
                print(f"⚠️ Ngrok keepalive check failed: {e}. Attempting restart...")
                with NGROK_LOCK:
                    NGROK_TUNNEL = None
                    PUBLIC_URL = None
                start_ngrok(port, prefer_pyngrok=True)
        elif not PUBLIC_URL:
           
            start_ngrok(port, prefer_pyngrok=True)


def initiate_outbound_call(to_number: str):
    """Uses the Twilio REST API to place an outbound call."""
    if not TWILIO_CLIENT or not TWILIO_PHONE:
        print("❌ Cannot initiate outbound call: Twilio client or phone number not set.")
        return

    if not PUBLIC_URL:
        print("❌ Cannot initiate outbound call without a PUBLIC_URL. Ensure ngrok or manual URL is set.")
        return


    call_url = f"{PUBLIC_URL}/outbound_voice"

    try:
        call = TWILIO_CLIENT.calls.create(
            to=to_number,
            from_=TWILIO_PHONE,
            url=call_url,
            timeout=30 
        )
        print(f"✅ Call initiated successfully. SID: {call.sid}")
        return call.sid
    except TwilioRestException as e:
        print(f"❌ Twilio API Error initiating call to {to_number}: {e}")
        return None
    except Exception as e:
        print(f"❌ Unknown error initiating call: {e}")
        return None


def console_outbound_trigger():     
    """CLI trigger to start an outbound call manually."""
    time.sleep(5) 
    print("\n--- Outbound Call Console Trigger ---")
    
    while True:
        target = input("Enter target number or 'd' for default: ").strip()
        if not target:
            continue
        
        if target.lower() == 'd':
            if OUTBOUND_TARGET_DEFAULT:
                target_number = OUTBOUND_TARGET_DEFAULT
            else:
                print("⚠️ OUTBOUND_TARGET_NUMBER not set in .env. Please enter a number.")
                continue
        else:
            target_number = target
        
        
        if not is_valid_e164(target_number):
            if re.match(r'^\d{10}$', target_number):
                target_number = f"+91{target_number}"
            else:
                print(f"❌ Invalid number format. Use E.164 (e.g., +919999999999).")
                continue

        print(f"Attempting outbound call to {target_number}...")
        initiate_outbound_call(target_number)

# ---------------- Main ----------------

if __name__ == "__main__":
    
    if TWILIO_SID and TWILIO_AUTH:
        ensure_indexes()
        create_dummy_doctors()
        load_doctors()
        load_session_history() 
    else:
        print("⚠️ Twilio credentials missing. Running in limited mode without voice calls.")

    try:
        # ---------- Start ngrok and keepalive threads ----------
        ngrok_thread = threading.Thread(target=start_ngrok, kwargs={'port': 5000, 'prefer_pyngrok': True}, daemon=True)
        ngrok_thread.start()

        keep_thread = threading.Thread(target=keep_ngrok_alive, kwargs={'port': 5000, 'interval': 15}, daemon=True)
        keep_thread.start()

        # ---------- Start console trigger for outbound calls ----------
        console_thread = threading.Thread(target=console_outbound_trigger, daemon=True)
        console_thread.start()

        print("🚀 Starting Flask server...")
        
        app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False) 
        print("✅ Flask server has stopped.")

    except KeyboardInterrupt:
        print("\nShutting down...")
        if 'ngrok' in globals() and 'NGROK_TUNNEL' in globals() and NGROK_TUNNEL:
            try:
                from pyngrok import ngrok
                ngrok.kill()
                print("Ngrok tunnels killed.")
            except ImportError:
                pass
        
    if mongo_client:
        mongo_client.close()
        print("MongoDB client closed.") #AK
