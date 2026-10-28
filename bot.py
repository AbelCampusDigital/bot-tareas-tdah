import os
import time
import logging
import threading
import calendar
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

# --- CONFIGURACIÓN DE ADMINISTRADOR (Opcional) ---
try:
    MI_TELEGRAM_ID = int(os.environ.get("MI_TELEGRAM_ID")) if os.environ.get("MI_TELEGRAM_ID") else None
except (TypeError, ValueError):
    MI_TELEGRAM_ID = None

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS usuarios_actividad (
                    chat_id BIGINT PRIMARY KEY,
                    ultimo_login TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)
            conn.commit()
            cur.close()
            conn.close()
            logger.info("✅ Supabase tables ready.")
            return True
        except Exception as e:
            logger.error(f"Error connecting to Supabase: {e}")
            time.sleep(2)
    return False

def registrar_o_actualizar_usuario(chat_id):
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usuarios_actividad (chat_id, ultimo_login) 
            VALUES (%s, NOW()) 
            ON CONFLICT (chat_id) 
            DO UPDATE SET ultimo_login = NOW();
        """, (chat_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error registrando actividad de usuario: {e}")

def ejecutar_rutina_limpieza_bd():
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT u.chat_id FROM usuarios_actividad u
            WHERE u.ultimo_login < NOW() - INTERVAL '1 day'
            AND NOT EXISTS (SELECT 1 FROM tareas t WHERE t.chat_id = u.chat_id);
        """)
        for row in cur.fetchall():
            uid = row[0]
            cur.execute("DELETE FROM usuarios_actividad WHERE chat_id = %s;", (uid,))
            logger.info(f"🧹 Limpieza: Usuario {uid} eliminado por inactividad (>1 día) sin tareas.")

        cur.execute("""
            SELECT u.chat_id FROM usuarios_actividad u
            WHERE u.ultimo_login < NOW() - INTERVAL '7 days'
            AND NOT EXISTS (SELECT 1 FROM tareas t WHERE t.chat_id = u.chat_id AND t.estado = 'pendiente');
        """)
        for row in cur.fetchall():
            uid = row[0]
            cur.execute("DELETE FROM tareas WHERE chat_id = %s;", (uid,))
            cur.execute("DELETE FROM usuarios_actividad WHERE chat_id = %s;", (uid,))
            logger.info(f"🧹 Limpieza: Usuario {uid} eliminado por inactividad (>7 días) con todo completado.")

        cur.execute("DELETE FROM usuarios_actividad WHERE ultimo_login < NOW() - INTERVAL '30 days';")

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error en rutina de limpieza de BD: {e}")

def db_guardar_tarea(chat_id, motivo, fecha_dt, rec_valor=None, rec_unidad=None, es_secuencial=False, intervalo_dias=0):
    if not DATABASE_URL:
        return False
    init_db()
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

def db_completar_tarea(tarea_id, chat_id):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT chat_id, motivo, fecha_vencimiento, recordatorio_valor, recordatorio_unidad, es_secuencial, intervalo_dias FROM tareas WHERE id = %s AND chat_id = %s;", (tarea_id, chat_id))
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

def db_hacer_secuencial(tarea_id, intervalo_dias, chat_id):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("UPDATE tareas SET es_secuencial = TRUE, intervalo_dias = %s WHERE id = %s AND chat_id = %s;", (intervalo_dias, tarea_id, chat_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error configurando tarea secuencial: {e}")
        return False

def db_eliminar_tarea(tarea_id, chat_id):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("DELETE FROM tareas WHERE id = %s AND chat_id = %s;", (tarea_id, chat_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error eliminando tarea: {e}")
        return False

# --- 2. ANTI-SLEEP / KEEP ALIVE SYSTEM ---
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
    url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/" if RENDER_EXTERNAL_URL else None
    while True:
        time.sleep(300)
        if url:
            try:
                r = requests.get(url, timeout=10)
                logger.info(f"🔄 Self-ping Render OK (Status: {r.status_code})")
            except Exception as e:
                logger.error(f"⚠️ Error en Self-ping Render: {e}")
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
def teclado_principal(es_admin=False):
    keyboard = [
        [InlineKeyboardButton("➕ 1. Añadir Tarea", callback_data="m1_crear")],
        [InlineKeyboardButton("📋 2. Listar Tareas", callback_data="m2_listar")],
        [InlineKeyboardButton("✏️ 3. Modificar Tareas", callback_data="m3_modificar")],
        [InlineKeyboardButton("🧩 4. IA: Desglose Anti-Parálisis", callback_data="m4_ia_desglose")],
        [InlineKeyboardButton("⚡ 5. IA: Coach Dopamina & Foco", callback_data="m5_ia_dopamina")],
        [InlineKeyboardButton("🔄 6. Reutilizar Tarea Secuencial", callback_data="m6_secuencial")]
    ]
    if es_admin:
        keyboard.append([InlineKeyboardButton("🛡️ Panel de Administrador", callback_data="menu_admin")])
    return InlineKeyboardMarkup(keyboard)

def teclado_admin():
    keyboard = [
        [InlineKeyboardButton("📊 1. Estadísticas de Usuarios", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 2. Listar Tareas de Otros", callback_data="admin_listar_otros")],
        [InlineKeyboardButton("🧹 3. Forzar Limpieza / Borrar", callback_data="admin_limpiar_db")],
        [InlineKeyboardButton("⬅️ Volver al Menú Principal", callback_data="menu_principal")]
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
    user_id = update.effective_user.id
    
    registrar_o_actualizar_usuario(user_id)
    ejecutar_rutina_limpieza_bd()

    es_admin = (MI_TELEGRAM_ID is not None and user_id == MI_TELEGRAM_ID)

    welcome_text = (
        f"🧠 ¡Hola! Soy tu bot Tareas_TDAH.\n"
        f"🆔 Tu ID de usuario es: {user_id}\n\n"
        f"Diseñado para reducir la carga mental, evitar la parálisis por análisis y mantener el foco.\n"
        f"Selecciona una opción del menú inferior para comenzar:"
    )
    await update.message.reply_text(
        text=welcome_text,
        reply_markup=teclado_principal(es_admin)
    )

async def procesar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    registrar_o_actualizar_usuario(user_id)
    es_admin = (MI_TELEGRAM_ID is not None and user_id == MI_TELEGRAM_ID)

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

    if query.data == "menu_principal":
        USER_STATES.pop(chat_id, None)
        await query.edit_message_text(
            "🧠 Menú Principal - Tareas_TDAH\nElige la acción a realizar:",
            reply_markup=teclado_principal(es_admin)
        )

    elif query.data == "menu_admin":
        if not es_admin:
            await query.answer("⛔ Acceso denegado. Solo para administradores.", show_alert=True)
            return
        await query.edit_message_text(
            "🛡️ Panel de Administración\nSelecciona una opción de gestión global:",
            reply_markup=teclado_admin()
        )

    elif query.data == "admin_stats":
        if not es_admin:
            return
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT chat_id) FROM usuarios_actividad;")
            total_usuarios = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM tareas;")
            total_tareas = cur.fetchone()[0]
            cur.execute("SELECT chat_id, ultimo_login FROM usuarios_actividad ORDER BY ultimo_login DESC LIMIT 10;")
            usuarios = cur.fetchall()
            cur.close()
            conn.close()

            msg = f"📊 Estadísticas Globales de Uso:\n\n" \
                  f"• Usuarios totales registrados: {total_usuarios}\n" \
                  f"• Tareas totales en BD: {total_tareas}\n\n" \
                  f"🕒 Últimos usuarios activos:\n"
            for u in usuarios:
                msg += f" - ID: {u[0]} | Acceso: {u[1].strftime('%d/%m/%Y %H:%M')}\n"

            kb = [[InlineKeyboardButton("⬅️ Volver a Admin", callback_data="menu_admin")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error obteniendo estadísticas: {e}", reply_markup=teclado_admin())

    elif query.data == "admin_listar_otros":
        if not es_admin:
            return
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT id, chat_id, motivo, estado, fecha_vencimiento FROM tareas ORDER BY creado_en DESC LIMIT 20;")
            tareas = cur.fetchall()
            cur.close()
            conn.close()

            msg = "👥 Listado global de tareas:\n\n"
            if not tareas:
                msg += "No hay tareas registradas en el sistema."
            else:
                for t in tareas:
                    msg += f"• T#{t[0]} [User: {t[1]}] - {t[2]} ({t[3]})\n"

            kb = [[InlineKeyboardButton("⬅️ Volver a Admin", callback_data="menu_admin")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error listando tareas: {e}", reply_markup=teclado_admin())

    elif query.data == "admin_limpiar_db":
        if not es_admin:
            return
        ejecutar_rutina_limpieza_bd()
        kb = [[InlineKeyboardButton("⬅️ Volver a Admin", callback_data="menu_admin")]]
        await query.edit_message_text("🧹 Limpieza forzada ejecutada con éxito.", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "m1_crear":
        await query.edit_message_text(
            "➕ Menú 1: Configurar Nueva Tarea\n"
            "Rellena los campos usando los botones antes de guardar:",
            reply_markup=teclado_wizard_fecha(wdata)
        )

    elif query.data == "w_set_dia":
        dias = list(range(1, 32))
        kb = [
            [InlineKeyboardButton(str(d).zfill(2), callback_data=f"select_dia_{d}") for d in dias[i:i+7]]
            for i in range(0, len(dias), 7)
        ]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="m1_crear")])
        await query.edit_message_text("Selecciona el Día (Máximo 31):", reply_markup=InlineKeyboardMarkup(kb))

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
            dia = int(wdata['dia'])
            mes = int(wdata['mes'])
            ano = int(wdata['ano'])
            max_dias = calendar.monthrange(ano, mes)[1]
            
            if dia > max_dias:
                await query.edit_message_text(
                    f"❌ Fecha no válida: El mes {mes}/{ano} solo tiene {max_dias} días.",
                    reply_markup=teclado_wizard_fecha(wdata)
                )
                return

            fecha_str = f"{ano}-{mes:02d}-{dia:02d} 09:00:00"
            fecha_dt = datetime.strptime(fecha_str, "%Y-%m-%d %H:%M:%S")
            rec_val = int(wdata["rec_num"]) if wdata.get("mostrar_alarma") else None
            rec_uni = wdata.get("rec_uni") if wdata.get("mostrar_alarma") else None

            motivo_final = wdata.get("motivo", "Sin definir")
            if motivo_final == "Sin definir":
                await context.bot.send_message(chat_id=chat_id, text="⚠️ Recuerda asignarle un nombre/motivo a la tarea.")
                return

            tid = db_guardar_tarea(chat_id, motivo_final, fecha_dt, rec_val, rec_uni)
            if tid:
                msg_confirm = (
                    f"✅ ¡Tarea Guardada con Éxito en tu apartado personal!\n\n"
                    f"📌 Motivo: {motivo_final}\n"
                    f"📅 Fecha: {dia:02d}/{mes:02d}/{ano}\n"
                )
                if rec_val:
                    msg_confirm += f"⏰ Alarma: {rec_val} {rec_uni} antes"
                
                context.user_data["wizard"] = {}
                await query.edit_message_text(msg_confirm, reply_markup=teclado_principal(es_admin))
            else:
                await query.edit_message_text("❌ Error guardando en Supabase.", reply_markup=teclado_principal(es_admin))
        except Exception as e:
            await query.edit_message_text(f"❌ Fecha u opción no válida: {e}", reply_markup=teclado_principal(es_admin))

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

    elif query.data.startswith("m2_") or query.data == "m2_listar":
        filtro = "todos"
        if "_" in query.data and query.data != "m2_listar":
            filtro = query.data.split("_")[1]

        tareas = db_obtener_tareas(chat_id, filtro)
        kb_filtros = [
            [
                InlineKeyboardButton(f"{'✅ ' if filtro=='dia' else ''}Hoy / Día", callback_data="m2_dia"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='semana' else ''}Semana", callback_data="m2_semana"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='mes' else ''}Mes", callback_data="m2_mes"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='todos' else ''}Todos", callback_data="m2_todos")
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
        res = db_completar_tarea(tid, chat_id)
        if res == "reprogramada":
            await context.bot.send_message(chat_id=chat_id, text=f"🔄 Tarea #{tid} completada y reprogramada automáticamente.")
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"🎉 ¡Tarea #{tid} completada! ¡Dopamina liberada! ⚡")
        await query.edit_message_text("✏️ Modificar Tareas", reply_markup=teclado_principal(es_admin))

    elif query.data.startswith("mod_elim_"):
        tid = int(query.data.split("_")[2])
        db_eliminar_tarea(tid, chat_id)
        await context.bot.send_message(chat_id=chat_id, text=f"🗑️ Tarea #{tid} eliminada.")
        await query.edit_message_text("✏️ Modificar Tareas", reply_markup=teclado_principal(es_admin))

    elif query.data == "m4_ia_desglose":
        tareas = db_obtener_tareas(chat_id, "todos")
        if not tareas:
            await query.edit_message_text("🧩 Menú 4: IA Anti-Parálisis\n\nNo tienes tareas pendientes para desglosar.", reply_markup=teclado_principal(es_admin))
            return

        kb = [[InlineKeyboardButton(f"🧩 Desglosar: {t[1][:25]}...", callback_data=f"ia_desg_{t[0]}")] for t in tareas[:5]]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")])
        await query.edit_message_text(
            "🧩 Menú 4: Desglose Inteligente TDAH (Micro-pasos)\n"
            "Elige la tarea que te da pereza o bloqueo:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif query.data.startswith("ia_desg_"):
        tid = int(query.data.split("_")[2])
        tareas = db_obtener_tareas(chat_id, "todos")
        tarea_sel = next((t for t in tareas if t[0] == tid), None)

        if tarea_sel:
            await context.bot.send_message(chat_id=chat_id, text="🧠 Gemini fragmentando tarea...")
            prompt = (
                f"Actúa como un experto en TDAH. La persona tiene bloqueada esta tarea: '{tarea_sel[1]}'. "
                f"Divide esta tarea en exactamente 3 micro-pasos absurdamente sencillos de menos de 5 minutos."
            )
            res = consultar_gemini(prompt)
            await context.bot.send_message(chat_id=chat_id, text=f"🧩 Plan Micro-pasos Anti-Bloqueo:\n\n{res}")

    elif query.data == "m5_ia_dopamina":
        tareas = db_obtener_tareas(chat_id, "todos")
        if not tareas:
            await query.edit_message_text("⚡ Menú 5: Coach de Dopamina\n\n¡No tienes tareas pendientes!", reply_markup=teclado_principal(es_admin))
            return

        resumen_tareas = "\n".join([f"- {t[1]} (Fecha: {t[2].strftime('%d/%m')})" for t in tareas[:7]])
        await context.bot.send_message(chat_id=chat_id, text="⚡ Calculando Puntos de Experiencia (XP)...")
        prompt = (
            f"Actúa como un Coach de Gamificación e Hiperfoco TDAH. Tareas pendientes:\n{resumen_tareas}\n"
            f"1. Asigna XP.\n2. Elige la tarea ideal para empezar AHORA.\n3. Mensaje corto de hiperfoco."
        )
        res = consultar_gemini(prompt)
        await context.bot.send_message(chat_id=chat_id, text=f"⚡ Sistema de Gamificación & Dopamina:\n\n{res}")

    elif query.data == "m6_secuencial":
        tareas = db_obtener_tareas(chat_id, "todos")
        if not tareas:
            await query.edit_message_text("🔄 Menú 6: Tareas Secuenciales\n\nNo tienes tareas creadas.", reply_markup=teclado_principal(es_admin))
            return

        kb = [[InlineKeyboardButton(f"🔄 Secuencial: {t[1][:25]}...", callback_data=f"sec_sel_{t[0]}")] for t in tareas[:5]]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")])
        await query.edit_message_text(
            "🔄 Menú 6: Convertir Tarea en Rutina Secuencial\n"
            "Selecciona una tarea para fijarle una frecuencia automática:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif query.data.startswith("sec_sel_"):
        tid = int(query.data.split("_")[2])
        kb = [
            [InlineKeyboardButton("📆 Diaria (Cada 1 día)", callback_data=f"sec_freq_{tid}_1")],
            [InlineKeyboardButton("📅 Semanal (Cada 7 días)", callback_data=f"sec_freq_{tid}_7")],
            [InlineKeyboardButton("🗓️ Mensual (Cada 30 días)", callback_data=f"sec_freq_{tid}_30")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="m6_secuencial")]
        ]
        await query.edit_message_text("Selecciona la frecuencia:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("sec_freq_"):
        parts = query.data.split("_")
        tid = int(parts[2])
        intervalo = int(parts[3])
        db_hacer_secuencial(tid, intervalo, chat_id)
        await query.edit_message_text(f"✅ ¡Tarea #{tid} configurada como Rutina Secuencial!", reply_markup=teclado_principal(es_admin))

# --- 6. TEXT INPUT HANDLER ---
async def manejar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in USER_STATES:
        return

    estado = USER_STATES.get(chat_id)

    if estado == "ESCRIBIENDO_MOTIVO":
        if "wizard" not in context.user_data:
            context.user_data["wizard"] = {}
        context.user_data["wizard"]["motivo"] = update.message.text
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            "✅ Motivo guardado. Pulsa sobre el menú para continuar:",
            reply_markup=teclado_wizard_fecha(context.user_data["wizard"])
        )

    elif estado == "ESCRIBIENDO_ALARMA_NUM":
        if "wizard" not in context.user_data:
            context.user_data["wizard"] = {}
        text = "".join(filter(str.isdigit, update.message.text))
        context.user_data["wizard"]["rec_num"] = text if text else "15"
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text(
            "✅ Número de alarma actualizado. Pulsa sobre el menú:",
            reply_markup=teclado_wizard_fecha(context.user_data["wizard"])
        )

# --- 7. MAIN APPLICATION ---
def main():
    logger.info("Iniciando Tareas_TDAH Bot...")
    init_db()

    threading.Thread(target=start_health_check, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    if not TOKEN:
        logger.critical("❌ ERROR: TELEGRAM_TOKEN no configurado.")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(procesar_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))

    logger.info("🚀 Bot activo y escuchando en Telegram.")
    app.run_polling()

if __name__ == "__main__":
    main()
