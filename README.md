# Bot de Telegram para Tareas TDAH 🧠⚡

Asistente personal en Telegram optimizado para personas con TDAH. Diseñado para reducir la carga mental, evitar la parálisis por análisis, mantener el foco mediante alertas directas en el móvil, gamificación y soporte de Inteligencia Artificial (Google Gemini).

---

## 🛠️ Tecnologías y Arquitectura

* **Python 3.x** con `python-telegram-bot` (ejecución mediante polling activo).
* **PostgreSQL (Supabase)** para persistencia de tareas y control de actividad de usuarios.
* **Servidor HTTP interno (Keep-Alive)** en hilo secundario para evitar la suspensión del servicio en el plan gratuito de Render.
* **Google Gemini API (`gemini-2.5-flash`)** para el desglose anti-parálisis y coach de dopamina.

---

## 🚀 Guía de Instalación y Despliegue Técnico

### Paso 1: Creación del Bot en Telegram
1. Abre Telegram y busca al usuario oficial **@BotFather**.
2. Envía el comando `/newbot`.
3. Sigue las instrucciones para asignarle un nombre visible y un nombre de usuario (ej: `TareasTdah_Bot`).
4. Copia el **HTTP API Token** generado. Esta será tu variable privada `TELEGRAM_TOKEN`.

---

### Paso 2: Base de Datos PostgreSQL (Supabase)
1. Entra en [supabase.com](https://supabase.com) y crea un nuevo proyecto.
2. Ve a **Project Settings -> Database** y copia la cadena de conexión en la pestaña **URI**.
3. Sustituye `[TU-CONTRASEÑA]` para formar tu variable `DATABASE_URL`:
   `postgresql://postgres.[REF]:[TU-CONTRASEÑA]@aws-0-eu-central-1.pooler.supabase.com:6543/postgres`
   *(Nota: El programa creará automáticamente las tablas necesarias al iniciar gracias a la función `init_db()`).*

---

### Paso 3: Clave de Inteligencia Artificial (Google AI Studio)
1. Entra en [Google AI Studio](https://aistudio.google.com/).
2. Haz clic en **Get API key** y genera una nueva clave.
3. Copia el código alfanumérico generado (`GEMINI_API_KEY`).

---

### Paso 4: Preparación del Repositorio en GitHub
Sube a la raíz de tu repositorio los siguientes archivos esenciales:

* **`requirements.txt`**:
  ```text
  python-telegram-bot
  psycopg2-binary
  requests
  beautifulsoup4
  google-genai
  ```
* **`bot.py`**: El código fuente principal del bot.

---

### Paso 5: Despliegue en la Nube (Render.com)
1. Conecta tu repositorio de GitHub en Render creando un nuevo **Web Service**.
2. Configura los parámetros de compilación:
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `python bot.py`
   * **Instance Type:** `Free`
3. Configura las siguientes **Environment Variables** en el panel de Render:

| Key | Value |
| :--- | :--- |
| **`TELEGRAM_TOKEN`** | Token de BotFather |
| **`DATABASE_URL`** | URI completa de Supabase |
| **`GEMINI_API_KEY`** | Clave de Google AI Studio |
| **`MI_TELEGRAM_ID`** | Tu ID numérico de Telegram (opcional, para panel de administración) |
| **`PORT`** | `10000` |
| **`RENDER_EXTERNAL_URL`** | `https://tu-app.onrender.com` |
| **`WEB_CONCURRENCY`** | `1` |

Haz clic en **Create Web Service** y el bot se pondrá en marcha automáticamente 24/7.