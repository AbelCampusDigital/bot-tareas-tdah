import os
import time
import logging
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import psycopg2
import requests
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# Configuration Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# State Management for User Text Inputs
USER_STATES = {}

# --- 1. SUPABASE DATABASE INITIALIZATION & MAINTENANCE ---
def init_db():
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL no configurada.")
        return False
    for _ in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tareas (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    motivo TEXT NOT NULL,
                    fecha_vencimiento TIMESTAMP WITH TIME ZONE NOT NULL,
                    recordatorio_valor INT,
                    recordatorio_unidad TEXT,
                    estado TEXT DEFAULT 'pendiente',
                    es_secuencial BOOLEAN DEFAULT FALSE,
                    intervalo_dias INT DEFAULT 0,
                    creado_en TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()
            cur.close()
            conn.close()
            logger.info("✅ Supabase 'tareas' table ready.")
            return True
        except Exception as e:
            logger.error(f"Error connecting to Supabase: {e}")
            time.sleep(2)
    return False

def db_guardar_tarea(chat_id, motivo, fecha_dt, rec_valor=None, rec_unidad=None, es_secuencial=False, intervalo_dias=0):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO tareas (chat_id, motivo, fecha_vencimiento, recordatorio_valor, recordatorio_unidad, es_secuencial, intervalo_dias) 
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;""",
            (chat_id, motivo, fecha_dt, rec_valor, rec_unidad, es_secuencial, intervalo_dias)
        )
        tarea_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return tarea_id
    except Exception as e:
        logger.error(f"Error guardando tarea: {e}")
        return False

def db_obtener_tareas(chat_id, filtro_tiempo="todos"):
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        ahora = datetime.now()
        
        query = "SELECT id, motivo, fecha_vencimiento, recordatorio_valor, recordatorio_unidad, estado, es_secuencial, intervalo_dias FROM tareas WHERE chat_id = %s AND estado = 'pendiente'"
        params = [chat_id]

        if filtro_tiempo == "dia":
            fin = ahora + timedelta(days=1)
            query += " AND fecha_vencimiento <= %s"
            params.append(fin)
        elif filtro_tiempo == "semana":
            fin = ahora + timedelta(days=7)
            query += " AND fecha_vencimiento <= %s"
            params.append(fin)
        elif filtro_tiempo == "mes":
            fin = ahora + timedelta(days=30)
            query += " AND fecha_vencimiento <= %s"
            params.append(fin)

        query += " ORDER BY fecha_vencimiento ASC;"
        cur.execute(query, tuple(params))
        filas = cur.fetchall()
        cur.close()
        conn.close()
        return filas
    except Exception as e:
        logger.error(f"Error consultando tareas: {e}")
        return []

def db_completar_tarea(tarea_id):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT chat_id, motivo, fecha_vencimiento, recordatorio_valor, recordatorio_unidad, es_secuencial, intervalo_dias FROM tareas WHERE id = %s;", (tarea_id,))
        t = cur.fetchone()
        
        if t and t[5]:
            intervalo = t[6] if t[6] > 0 else 1
            nueva_fecha = datetime.now() + timedelta(days=intervalo)
            cur.execute("UPDATE tareas SET fecha_vencimiento = %s WHERE id = %s;", (nueva_fecha, tarea_id))
            conn.commit()
            cur.close()
            conn.close()
            return "reprogramada"
        else:
            cur.execute("UPDATE tareas SET estado = 'completada' WHERE id = %s;", (tarea_id,))
            conn.commit()
            cur.close()
            conn.close()
            return "completada"
    except Exception as e:
        logger.error(f"Error completando tarea: {e}")
        return False

def db_hacer_secuencial(tarea_id, intervalo_dias):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("UPDATE tareas SET es_secuencial = TRUE, intervalo_dias = %s WHERE id = %s;", (intervalo_dias, tarea_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error configurando tarea secuencial: {e}")
        return False

def db_eliminar_tarea(tarea_id):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("DELETE FROM tareas WHERE id = %s;", (tarea_id,))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error eliminando tarea: {e}")
        return False

# --- 2. ANTI-SLEEP / KEEP ALIVE SYSTEM (RENDER & SUPABASE) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Tareas_TDAH Bot Activo 🧠⚡".encode("utf-8"))
    def log_message(self, format, *args):
        return

def start_health_check():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def keep_alive_ping():
    """Mantiene despierto el servicio web de Render y hace consultas periódicas a Supabase para evitar pausas."""
    url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/" if RENDER_EXTERNAL_URL else None
    
    while True:
        time.sleep(300) # Cada 5 minutos (evita que Render duerma a los 15 min)
        # 1. Ping al servidor HTTP de Render
        if url:
            try:
                r = requests.get(url, timeout=10)
                logger.info(f"🔄 Self-ping Render OK (Status: {r.status_code})")
            except Exception as e:
                logger.error(f"⚠️ Error en Self-ping Render: {e}")
        
        # 2. Ping liviano a Supabase para mantener la BD activa
        if DATABASE_URL:
            try:
                conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
                cur = conn.cursor()
                cur.execute("SELECT 1;")
                cur.close()
                conn.close()
                logger.info("🔄 Ping Supabase Database OK")
            except Exception as e:
                logger.error(f"⚠️ Error en Keep-alive Supabase: {e}")

# --- 3. GEMINI IA INTEGRATION ---
def consultar_gemini(prompt):
    if not GEMINI_API_KEY:
        return "⚠️ Configura GEMINI_API_KEY en Render para usar las funciones de IA."
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text if response and response.text else "⚠️ Sin respuesta de la IA."
    except Exception as e:
        return f"❌ Error llamando a Gemini IA: {str(e)}"

# --- 4. TELEGRAM KEYBOARDS & MENUS ---
def teclado_principal():
    keyboard = [
        [InlineKeyboardButton("➕ 1. Añadir Tarea", callback_data="m1_crear")],
        [InlineKeyboardButton("📋 2. Listar Tareas", callback_data="m2_listar")],
        [InlineKeyboardButton("✏️ 3. Modificar Tareas", callback_data="m3_modificar")],
        [InlineKeyboardButton("🧩 4. IA: Desglose Anti-Parálisis", callback_data="m4_ia_desglose")],
        [InlineKeyboardButton("⚡ 5. IA: Coach Dopamina & Foco", callback_data="m5_ia_dopamina")],
        [InlineKeyboardButton("🔄 6. Reutilizar Tarea Secuencial", callback_data="m6_secuencial")]
    ]
    return InlineKeyboardMarkup(keyboard)

def teclado_wizard_fecha(datos):
    dia = datos.get("dia", "--")
    mes = datos.get("mes", "--")
    ano = datos.get("ano", "--")
    motivo = datos.get("motivo", "Pulsar para escribir")

    kb = [
        [
            InlineKeyboardButton(f"📅 Día: {dia}", callback_data="w_set_dia"),
            InlineKeyboardButton(f"📆 Mes: {mes}", callback_data="w_set_mes"),
            InlineKeyboardButton(f"🗓️ Año: {ano}", callback_data="w_set_ano")
        ],
        [InlineKeyboardButton(f"📝 Motivo: {motivo}", callback_data="w_set_motivo")],
        [
            InlineKeyboardButton("➕ Añadir la Tarea", callback_data="w_guardar_tarea"),
            InlineKeyboardButton("📅 Añadir al Calendario", callback_data="w_generar_ical"),
        ],
        [InlineKeyboardButton("⏰ Añadir una Alarma", callback_data="w_toggle_alarma")]
    ]

    if datos.get("mostrar_alarma", False):
        rec_num = datos.get("rec_num", "15")
        rec_uni = datos.get("rec_uni", "minutos")
        kb.append([
            InlineKeyboardButton(f"🔢 Recordatorio: [{rec_num}]", callback_data="w_set_rec_num"),
            InlineKeyboardButton(f"{'✅ ' if rec_uni=='minutos' else ''}Minutos", callback_data="w_uni_minutos"),
            InlineKeyboardButton(f"{'✅ ' if rec_uni=='horas' else ''}Horas", callback_data="w_uni_horas"),
            InlineKeyboardButton(f"{'✅ ' if rec_uni=='dias' else ''}Días", callback_data="w_uni_dias")
        ])

    kb.append([InlineKeyboardButton("⬅️ Cancelar / Volver", callback_data="menu_principal")])
    return InlineKeyboardMarkup(kb)

# --- 5. HANDLERS TELEGRAM ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🧠 ¡Bienvenido a Tareas_TDAH Bot!\n\n"
        "Diseñado para reducir la carga mental, evitar la parálisis por análisis y mantener el foco.\n"
        "Selecciona una opción del menú inferior para comenzar:"
    )
    await update.message.reply_text(
        text=welcome_text,
        reply_markup=teclado_principal()
    )

async def procesar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if "wizard" not in context.user_data:
        context.user_data["wizard"] = {
            "dia": str(datetime.now().day).zfill(2),
            "mes": str(datetime.now().month).zfill(2),
            "ano": str(datetime.now().year),
            "motivo": "Sin definir",
            "mostrar_alarma": False,
            "rec_num": "15",
            "rec_uni": "minutos"
        }

    wdata = context.user_data["wizard"]

    # MENÚ PRINCIPAL
    if query.data == "menu_principal":
        USER_STATES.pop(chat_id, None)
        await query.edit_message_text(
            "🧠 Menú Principal - Tareas_TDAH\nElige la acción a realizar:",
            reply_markup=teclado_principal()
        )

    # --- MENÚ 1: AÑADIR TAREA ---
    elif query.data == "m1_crear":
        await query.edit_message_text(
            "➕ Menú 1: Configurar Nueva Tarea\n"
            "Rellena los campos usando los botones antes de guardar:",
            reply_markup=teclado_wizard_fecha(wdata)
        )

    elif query.data == "w_set_dia":
        kb = [[InlineKeyboardButton(str(d).zfill(2), callback_data=f"select_dia_{d}") for d in range(r, r+7)] for r in range(1, 31, 7)]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="m1_crear")])
        await query.edit_message_text("Selecciona el Día:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("select_dia_"):
        d = query.data.split("_")[2]
        wdata["dia"] = str(d).zfill(2)
        await query.edit_message_text("➕ Configurar Nueva Tarea", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_mes":
        kb = [[InlineKeyboardButton(str(m).zfill(2), callback_data=f"select_mes_{m}") for m in range(r, r+4)] for r in range(1, 13, 4)]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="m1_crear")])
        await query.edit_message_text("Selecciona el Mes:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("select_mes_"):
        m = query.data.split("_")[2]
        wdata["mes"] = str(m).zfill(2)
        await query.edit_message_text("➕ Configurar Nueva Tarea", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_ano":
        a_actual = datetime.now().year
        kb = [[InlineKeyboardButton(str(a), callback_data=f"select_ano_{a}") for a in range(a_actual, a_actual+3)]]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="m1_crear")])
        await query.edit_message_text("Selecciona el Año:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("select_ano_"):
        a = query.data.split("_")[2]
        wdata["ano"] = str(a)
        await query.edit_message_text("➕ Configurar Nueva Tarea", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_motivo":
        USER_STATES[chat_id] = "ESCRIBIENDO_MOTIVO"
        await context.bot.send_message(chat_id=chat_id, text="✍️ Responde a este mensaje escribiendo el motivo/nombre de la tarea:")

    elif query.data == "w_toggle_alarma":
        wdata["mostrar_alarma"] = not wdata.get("mostrar_alarma", False)
        await query.edit_message_text("➕ Configurar Nueva Tarea", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data in ["w_uni_minutos", "w_uni_horas", "w_uni_dias"]:
        wdata["rec_uni"] = query.data.replace("w_uni_", "")
        await query.edit_message_text("➕ Configurar Nueva Tarea", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_rec_num":
        USER_STATES[chat_id] = "ESCRIBIENDO_ALARMA_NUM"
        await context.bot.send_message(chat_id=chat_id, text="🔢 Escribe en un mensaje el número para la alarma (ej. 10, 15, 30):")

    elif query.data == "w_guardar_tarea":
        try:
            fecha_str = f"{wdata['ano']}-{wdata['mes']}-{wdata['dia']} 09:00:00"
            fecha_dt = datetime.strptime(fecha_str, "%Y-%m-%d %H:%M:%S")
            rec_val = int(wdata["rec_num"]) if wdata.get("mostrar_alarma") else None
            rec_uni = wdata.get("rec_uni") if wdata.get("mostrar_alarma") else None

            tid = db_guardar_tarea(chat_id, wdata["motivo"], fecha_dt, rec_val, rec_uni)
            if tid:
                msg_confirm = (
                    f"✅ ¡Tarea Guardada con Éxito!\n\n"
                    f"📌 Motivo: {wdata['motivo']}\n"
                    f"📅 Fecha: {wdata['dia']}/{wdata['mes']}/{wdata['ano']}\n"
                )
                if rec_val:
                    msg_confirm += f"⏰ Alarma: {rec_val} {rec_uni} antes"
                
                await query.edit_message_text(msg_confirm, reply_markup=teclado_principal())
                context.user_data["wizard"] = {}
            else:
                await query.edit_message_text("❌ Error guardando en Supabase.", reply_markup=teclado_principal())
        except Exception as e:
            await query.edit_message_text(f"❌ Fecha no válida: {e}", reply_markup=teclado_principal())

    elif query.data == "w_generar_ical":
        try:
            fecha_str = f"{wdata['ano']}{wdata['mes']}{wdata['dia']}T090000Z"
            gcal_link = f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={requests.utils.quote(wdata['motivo'])}&dates={fecha_str}/{fecha_str}"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📅 Añadir a Google Calendar:\n{gcal_link}"
            )
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error generando enlace: {e}")

    # --- MENÚ 2: LISTAR TAREAS ---
    elif query.data.startswith("m2_") or query.data == "m2_listar":
        filtro = "todos"
        if "_" in query.data and query.data != "m2_listar":
            filtro = query.data.split("_")[1]

        tareas = db_obtener_tareas(chat_id, filtro)
        kb_filtros = [
            [
                InlineKeyboardButton(f"{'✅ ' if filtro=='dia' else ''}Hoy / Día", callback_data="m2_dia"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='semana' else ''}Semana", callback_data="m2_semana"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='mes' else ''}Mes", callback_data="m2_mes")
            ],
            [InlineKeyboardButton("⬅️ Volver al Menú", callback_data="menu_principal")]
        ]

        if not tareas:
            msg = f"📋 Menú 2: Listar Tareas ({filtro.upper()})\n\n🎉 No tienes tareas pendientes en este rango."
        else:
            msg = f"📋 Menú 2: Listar Tareas ({filtro.upper()})\nOrdenadas por cercanía (prioridad TDAH):\n\n"
            for t in tareas:
                f_str = t[2].strftime("%d/%m/%Y")
                alarm_str = f"{t[3]}{t[4][0]}" if t[3] else "-"
                sec_str = " 🔄 (Secuencial)" if t[6] else ""
                msg += f"• #{t[0]} [{f_str}] {t[1]} (Alarma: {alarm_str}){sec_str}\n"

        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb_filtros))

    # --- MENÚ 3: MODIFICAR TAREAS ---
    elif query.data.startswith("m3_") or query.data == "m3_modificar":
        filtro = "todos"
        if query.data.startswith("m3_f_"):
            filtro = query.data.replace("m3_f_", "")

        tareas = db_obtener_tareas(chat_id, filtro)
        kb = [
            [
                InlineKeyboardButton(f"{'✅ ' if filtro=='dia' else ''}Día", callback_data="m3_f_dia"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='semana' else ''}Semana", callback_data="m3_f_semana"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='mes' else ''}Mes", callback_data="m3_f_mes")
            ]
        ]

        msg = "✏️ Menú 3: Modificar Tareas\nSelecciona la tarea a completar o eliminar:\n\n"
        if not tareas:
            msg += "✨ No hay tareas pendientes."
        else:
            for t in tareas:
                f_str = t[2].strftime("%d/%m")
                sec_str = " 🔄" if t[6] else ""
                msg += f"📌 #{t[0]} [{f_str}] {t[1]}{sec_str}\n"
                kb.append([
                    InlineKeyboardButton(f"✅ Completar #{t[0]}", callback_data=f"mod_comp_{t[0]}"),
                    InlineKeyboardButton(f"🗑️ Eliminar #{t[0]}", callback_data=f"mod_elim_{t[0]}")
                ])

        kb.append([InlineKeyboardButton("⬅️ Volver al Menú", callback_data="menu_principal")])
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("mod_comp_"):
        tid = int(query.data.split("_")[2])
        res = db_completar_tarea(tid)
        if res == "reprogramada":
            await context.bot.send_message(chat_id=chat_id, text=f"🔄 Tarea #{tid} completada y reprogramada automáticamente para el siguiente ciclo.")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"🎉 ¡Tarea #{tid} completada! ¡Dopamina liberada! ⚡")
        await query.edit_message_text("✏️ Modificar Tareas", reply_markup=teclado_principal())

    elif query.data.startswith("mod_elim_"):
        tid = int(query.data.split("_")[2])
        db_eliminar_tarea(tid)
        await context.bot.send_message(chat_id=chat_id, text=f"🗑️ Tarea #{tid} eliminada.")
        await query.edit_message_text("✏️ Modificar Tareas", reply_markup=teclado_principal())

    # --- MENÚ 4: IA (DESGLOSE ANTI-PARÁLISIS TDAH) ---
    elif query.data == "m4_ia_desglose":
        tareas = db_obtener_tareas(chat_id, "todos")
        if not tareas:
            await query.edit_message_text("🧩 Menú 4: IA Anti-Parálisis\n\nNo tienes tareas pendientes para desglosar.", reply_markup=teclado_principal())
            return

        kb = [[InlineKeyboardButton(f"🧩 Desglosar: {t[1][:25]}...", callback_data=f"ia_desg_{t[0]}")] for t in tareas[:5]]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")])
        await query.edit_message_text(
            "🧩 Menú 4: Desglose Inteligente TDAH (Micro-pasos)\n"
            "Elige la tarea que te da pereza o bloqueo. La IA la dividirá en 3 acciones de 3 minutos:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif query.data.startswith("ia_desg_"):
        tid = int(query.data.split("_")[2])
        tareas = db_obtener_tareas(chat_id, "todos")
        tarea_sel = next((t for t in tareas if t[0] == tid), None)

        if tarea_sel:
            await context.bot.send_message(chat_id=chat_id, text="🧠 Gemini fragmentando tarea... Por favor espera unos segundos.")
            prompt = (
                f"Actúa como un experto en TDAH y disfunción ejecutiva. "
                f"La persona tiene bloqueada esta tarea: '{tarea_sel[1]}'. "
                f"Divide esta tarea en exactamente 3 micro-pasos absurdamente sencillos de menos de 5 minutos. "
                f"Formato breve con emojis. Ánimo conciso y positivo."
            )
            res = consultar_gemini(prompt)
            await context.bot.send_message(chat_id=chat_id, text=f"🧩 Plan Micro-pasos Anti-Bloqueo:\n\n{res}")

    # --- MENÚ 5: IA (COACH DOPAMINA & FOCO) ---
    elif query.data == "m5_ia_dopamina":
        tareas = db_obtener_tareas(chat_
