# 🥗 Raw Diet — Personal Trainer AI Chatbot

An AI-powered personal trainer and nutrition coach chatbot built for the **Raw Diet** app. It acts as a dedicated diet and fitness expert, giving personalised advice based on each user's profile (height, weight, diet type, health conditions, activity level) fetched live from the Raw Diet backend.

---

## ✨ Features

| Feature | Details |
|---|---|
| **Streaming chat** | Real-time token-by-token streaming via SSE (Server-Sent Events) |
| **Voice input** | Record voice → transcribe via Gemini → personalised answer |
| **Voice response (TTS)** | Answer spoken back via Google Cloud Text-to-Speech |
| **Personalised responses** | Fetches user's profile from Raw Diet API using Firebase JWT |
| **Personal trainer persona** | Suggests diet plans, meal ideas, calorie targets, supplement guidance |
| **Health-aware** | Adapts advice based on health conditions (diabetes, hypertension, etc.) |
| **Diet-type aware** | Respects veg / vegan / non-veg / jain / eggetarian preferences |
| **Allergy-safe** | Never suggests foods the user is allergic to |
| **Chat history** | Multi-turn conversation memory (persisted in PostgreSQL if configured) |
| **Built-in Web UI** | Beautiful single-file HTML chatbot UI served at `/ui` |

---

## 🏗️ Architecture

```
rawdiet-chatbot/
├── main.py            ← FastAPI app entry point
├── chat.py            ← /chat and /chat/stream endpoints
├── voice_chat.py      ← /voice and /voice/stream endpoints
├── rag_engine.py      ← Gemini AI + user profile fetching + prompt building
├── models.py          ← SQLAlchemy Chat model
├── database.py        ← DB engine setup (optional)
├── requirements.txt
├── Dockerfile
├── .env.example
├── .github/
│   └── workflows/
│       └── deploy.yml ← Cloud Run CI/CD
└── static/
    └── index.html     ← Built-in web chatbot UI
```

---

## 🚀 Quick Start (Local)

### 1. Clone & install

```bash
git clone <your-repo>
cd rawdiet-chatbot
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set environment variables

```bash
cp .env.example .env
# Edit .env and fill in GEMINI_API_KEY
```

### 3. Run

```bash
uvicorn main:app --reload --port 8080
```

Open **http://localhost:8080/ui** to use the chatbot UI.
API docs at **http://localhost:8080/docs**.

---

## 🌐 API Endpoints

### Chat

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/chat/` | Non-streaming chat |
| `POST` | `/chat/stream` | **SSE streaming chat** (recommended) |
| `GET`  | `/chat/history` | Get conversation history |

#### Request (form-data)

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | ✅ | User's message |
| `user_id` | string | — | Firebase UID (optional, for logging) |
| `session_id` | string | — | Session ID (returned in response; persist and re-send) |

#### Headers

| Header | Description |
|---|---|
| `Authorization: Bearer <token>` | Firebase JWT — used to fetch user's profile from Raw Diet API |

#### SSE Event Contract (`/chat/stream`)

```jsonc
// Token arriving
{"type": "chunk", "text": "Here are some meal ideas..."}

// Stream complete
{"type": "done",  "text": "<full answer>", "session_id": "abc123"}

// Error
{"type": "error", "text": "Something went wrong"}
```

---

### Voice

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/voice/` | Non-streaming voice (returns JSON + base64 MP3) |
| `POST` | `/voice/stream` | **SSE streaming voice** (recommended) |

#### Request (multipart/form-data)

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | audio file | ✅ | Audio recording (webm, mp3, wav, ogg, m4a) |
| `user_id` | string | — | Firebase UID |
| `session_id` | string | — | Session ID |
| `response_format` | string | — | `json` (default) or `audio` (raw MP3 bytes) |

#### SSE Voice Event Contract (`/voice/stream`)

```jsonc
// Token arriving
{"type": "chunk", "text": "Great question!..."}

// Stream complete — includes TTS audio
{
  "type": "done",
  "text": "<full answer>",
  "session_id": "abc123",
  "user_said": "<transcription of user's audio>",
  "audio_base64": "<base64-encoded MP3>"
}
```

---

## 🔐 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | Google Gemini API key |
| `RAW_DIET_API_BASE` | — | Raw Diet backend URL (default: production URL) |
| `DATABASE_URL` | — | PostgreSQL URL for chat persistence |
| `ALLOWED_ORIGINS` | — | Comma-separated CORS origins |
| `DEBUG` | — | `true` to show detailed error messages |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to GCP service account JSON (for TTS locally) |

---

## 🏥 User Profile Personalisation

When a Firebase JWT is passed in the `Authorization` header, the chatbot:
1. Calls `GET /api/users/me` on the Raw Diet backend with that token
2. Extracts the user's: **name, age, gender, height, weight, diet type, activity level, allergies, health conditions**
3. Calculates BMI automatically
4. Injects all of this into the Gemini prompt as personalised context

Without a token, it still works — just as a general diet/fitness expert.

---

## 🐳 Docker

```bash
# Build
docker build -t rawdiet-chatbot .

# Run locally
docker run -p 8080:8080 \
  -e GEMINI_API_KEY=your_key \
  -e RAW_DIET_API_BASE=https://test---raw-diet-backend-5rnsarrnya-uc.a.run.app \
  rawdiet-chatbot
```

---

## ☁️ Deploy to Cloud Run

### Prerequisites
- GCP project with Cloud Run, Artifact Registry enabled
- GitHub secrets: `GCP_PROJECT_ID`, `GCP_SA_KEY`, `GCP_SERVICE_ACCOUNT`
- Cloud Run secrets: `GEMINI_API_KEY`, `DATABASE_URL`

### Deploy

```bash
# Manual deploy
gcloud run deploy rawdiet-chatbot \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets="GEMINI_API_KEY=GEMINI_API_KEY:latest"
```

Or push to `main` branch — GitHub Actions will deploy automatically.

---

## 📱 Integrating in the App (React Native / Expo)

### Text chat (streaming)

```javascript
async function askTrainer(message, firebaseToken, sessionId) {
  const formData = new FormData();
  formData.append('text', message);
  formData.append('user_id', 'user_firebase_uid');
  if (sessionId) formData.append('session_id', sessionId);

  const response = await fetch('https://your-chatbot-url/chat/stream', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${firebaseToken}` },
    body: formData,
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const event = JSON.parse(line.slice(6));
      if (event.type === 'chunk') {
        // Append token to UI
        appendToken(event.text);
      } else if (event.type === 'done') {
        // Save session_id for next turn
        saveSessionId(event.session_id);
      }
    }
  }
}
```

### Voice chat (streaming)

```javascript
async function sendVoice(audioBlob, firebaseToken, sessionId) {
  const formData = new FormData();
  formData.append('file', audioBlob, 'voice.webm');
  formData.append('user_id', 'user_firebase_uid');
  if (sessionId) formData.append('session_id', sessionId);

  const response = await fetch('https://your-chatbot-url/voice/stream', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${firebaseToken}` },
    body: formData,
  });

  // Same SSE parsing as above
  // done event includes: user_said (transcription) + audio_base64 (MP3)
}
```

---

## 🎯 What the chatbot can help users with

- 🏋️ **Goal-based diet plans** — weight loss, weight gain, muscle building
- 🥗 **Personalised meal ideas** — based on diet type and allergies
- 📊 **Calorie & macro targets** — calculated from user's body stats
- 💊 **Supplement guidance** — protein, vitamins, when relevant
- 😣 **Post-meal health issues** — "I ate X and feel Y"
- 🩺 **Health-condition nutrition** — diabetes-friendly, BP-friendly meals
- 💧 **Hydration & recovery** — post-workout, daily water intake
- 🥦 **Raw & whole food principles** — the core of the Raw Diet philosophy
- 📅 **Meal timing & intermittent fasting** — schedule planning
- 📖 **Food label reading** — ingredients, hidden sugars, macros

---

## 📝 Notes

- The chatbot does **not** collect emails or schedule meetings — it's purely a diet/health assistant
- It shares the same session system (`session_id` cookie + form field) so future mobile app integration is straightforward
- TTS uses **Google Cloud Neural2** voices — requires a GCP service account with TTS API enabled
- Without TTS credentials the chatbot still works; only voice response audio is disabled
