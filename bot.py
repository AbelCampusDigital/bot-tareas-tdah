import os
import time
import logging
import threading
import asyncio
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
                    notificado BOOLEAN DEFAULT FALSE,
                    creado_en TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                ALTER TABLE tareas ADD COLUMN IF NOT EXISTS notificado BOOLEAN DEFAULT FALSE;
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

        cur.execute("""
            SELECT u.chat_id FROM usuarios_actividad u
            WHERE u.ultimo_login < NOW() - INTERVAL '7 days'
            AND NOT EXISTS (SELECT 1 FROM tareas t WHERE t.chat_id = u.chat_id AND t.estado = 'pendiente');
        """)
        for row in cur.fetchall():
            uid = row[0]
            cur.execute("DELETE FROM tareas WHERE chat_id = %s;", (uid,))
            cur.execute("DELETE FROM usuarios_actividad WHERE chat_id = %s;", (uid,))

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
            """INSERT INTO tareas (chat_id, motivo, fecha_vencimiento, recordatorio_valor, recordatorio_unidad, es_secuencial, intervalo_dias, notificado) 
               VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE) RETURNING id;""",
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
            cur.execute("UPDATE tareas SET fecha_vencimiento = %s, notificado = FALSE WHERE id = %s;", (nueva_fecha, tarea_id))
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

def db_actualizar_alarma_tarea(tarea_id, chat_id, rec_valor, rec_unidad):
    if not DATABASE_URL:
        return False
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("UPDATE tareas SET recordatorio_valor = %s, recordatorio_unidad = %s, notificado = FALSE WHERE id = %s AND chat_id = %s;", (rec_valor, rec_unidad, tarea_id, chat_id))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error actualizando alarma de tarea: {e}")
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

# --- 2. ANTI-SLEEP & ALARM DISPATCHER SYSTEM ---
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
                requests.get(url, timeout=10)
            except Exception:
                pass
        if DATABASE_URL:
            try:
                conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
                cur = conn.cursor()
                cur.execute("SELECT 1;")
                cur.close()
                conn.close()
            except Exception:
                pass

def verificar_y_enviar_alertas(application):
    while True:
        time.sleep(60)
        if not DATABASE_URL:
            continue
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, chat_id, motivo, fecha_vencimiento, recordatorio_valor, recordatorio_unidad 
                FROM tareas 
                WHERE estado = 'pendiente' AND notificado = FALSE AND recordatorio_valor IS NOT NULL;
            """)
            tareas_pendientes = cur.fetchall()

            for t in tareas_pendientes:
                tid, chat_id, motivo, f_venc, rec_val, rec_uni = t[0], t[1], t[2], t[3], t[4], t[5]
                
                delta = timedelta(0)
                if rec_uni == "minutos":
                    delta = timedelta(minutes=rec_val)
                elif rec_uni == "horas":
                    delta = timedelta(hours=rec_val)
                elif rec_uni == "dias":
                    delta = timedelta(days=rec_val)

                tiempo_aviso = f_venc - delta

                if datetime.now(f_venc.tzinfo) >= tiempo_aviso:
                    kb = [
                        [InlineKeyboardButton("✅ Completar Tarea", callback_data=f"mod_comp_{tid}")],
                        [InlineKeyboardButton("📋 Ver Mis Tareas", callback_data="m2_listar")]
                    ]
                    mensaje_alerta = (
                        "🚨🔔 ALARMA DE TAREA TDAH!\n\n"
                        f"📌 Tarea: {motivo}\n"
                        f"📅 Vence el: {f_venc.strftime('%d/%m/%Y a las %H:%M')}\n\n"
                        "¡Actúa ahora para mantener el foco! ⚡"
                    )
                    try:
                        application.bot.send_message(
                            chat_id=chat_id,
                            text=mensaje_alerta,
                            reply_markup=InlineKeyboardMarkup(kb)
                        )
                        cur.execute("UPDATE tareas SET notificado = TRUE WHERE id = %s;", (tid,))
                        conn.commit()
                    except Exception as e:
                        logger.error(f"Error enviando alerta: {e}")

            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"Error en bucle de alertas: {e}")

# --- 3. GEMINI IA INTEGRATION (Non-blocking & Robust) ---
def consultar_gemini(prompt):
    if not GEMINI_API_KEY:
        return "⚠️ Configura GEMINI_API_KEY en Render para usar las funciones de IA."
    
    modelos_a_probar = ["gemini-1.5-flash", "gemini-2.5-flash", "gemini-3.8-flash"]
    client = genai.Client(api_key=GEMINI_API_KEY)
    ultimo_error = ""
    
    for modelo in modelos_a_probar:
        try:
            response = client.models.generate_content(
                model=modelo,
                contents=prompt
            )
            if response and response.text:
                return response.text
        except Exception as e:
            ultimo_error = str(e)
            continue
            
    return f"❌ Error llamando a Gemini IA: {ultimo_error}"

# --- 4. TELEGRAM KEYBOARDS & MENUS ---
def teclado_principal(es_admin=False):
    keyboard = [
        [InlineKeyboardButton("➕ 1. Añadir Tarea", callback_data="m1_crear")],
        [InlineKeyboardButton("📋 2. Listar Tareas", callback_data="m2_listar")],
        [InlineKeyboardButton("⏰ 3. Listar Alarmas", callback_data="m3_alarmas")],
        [InlineKeyboardButton("✏️ 4. Modificar Tareas", callback_data="m4_modificar")],
        [InlineKeyboardButton("🧩 5. IA: Desglose Anti-Parálisis", callback_data="m5_ia_desglose")],
        [InlineKeyboardButton("⚡ 6. IA: Coach Dopamina & Foco", callback_data="m6_ia_dopamina")],
        [InlineKeyboardButton("🔄 7. Reutilizar Tarea Secuencial", callback_data="m7_secuencial")],
        [InlineKeyboardButton("ℹ️ 8. About", callback_data="m8_about")]
    ]
    if es_admin:
        keyboard.append([InlineKeyboardButton("🛡️ Panel de Administrador", callback_data="menu_admin")])
    return InlineKeyboardMarkup(keyboard)

def teclado_admin():
    keyboard = [
        [InlineKeyboardButton("📊 1. Estadísticas de Usuarios", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 2. Listar Tareas de Otros", callback_data="admin_listar_otros")],
        [InlineKeyboardButton("🧪 3. Pruebas de Alarma", callback_data="admin_pruebas")],
        [InlineKeyboardButton("🧹 4. Forzar Limpieza / Borrar", callback_data="admin_limpiar_db")],
        [InlineKeyboardButton("⬅️ Volver al Menú Principal", callback_data="menu_principal")]
    ]
    return InlineKeyboardMarkup(keyboard)

def teclado_wizard_fecha(datos):
    dia = datos.get("dia", "--")
    mes = datos.get("mes", "--")
    ano = datos.get("ano", "--")
    hora = datos.get("hora", "09")
    minuto = datos.get("minuto", "00")
    ampm = datos.get("ampm", "AM")
    motivo = datos.get("motivo", "Pulsar para escribir")

    try:
        h_12 = int(hora)
        m_val = int(minuto)
        if ampm == "PM" and h_12 < 12:
            h_24 = h_12 + 12
        elif ampm == "AM" and h_12 == 12:
            h_24 = 0
        else:
            h_24 = h_12
        hora_visor = f"{h_24:02d}:{m_val:02d} horas"
    except Exception:
        hora_visor = f"{hora}:{minuto} {ampm}"

    kb = [
        [
            InlineKeyboardButton(f"📅 Día: {dia}", callback_data="w_set_dia"),
            InlineKeyboardButton(f"📆 Mes: {mes}", callback_data="w_set_mes"),
            InlineKeyboardButton(f"🗓️ Año: {ano}", callback_data="w_set_ano")
        ],
        [
            InlineKeyboardButton(f"🕒 Hora: {hora_visor}", callback_data="w_set_hora_menu")
        ],
        [InlineKeyboardButton(f"📝 Motivo: {motivo}", callback_data="w_set_motivo")],
        [
            InlineKeyboardButton("➕ Añadir la Tarea", callback_data="w_guardar_tarea"),
            InlineKeyboardButton("📅 Google Calendar", callback_data="w_generar_ical"),
        ],
        [InlineKeyboardButton("⏰ Alarma Móvil", callback_data="w_toggle_alarma")]
    ]

    if datos.get("mostrar_alarma", False):
        rec_val = datos.get("rec_val", 15)
        kb.append([
            InlineKeyboardButton(f"{'✅ ' if rec_val==0 else ''}En hora", callback_data="alarm_val_0"),
            InlineKeyboardButton(f"{'✅ ' if rec_val==15 else ''}15MntAntes", callback_data="alarm_val_15")
        ])
        kb.append([
            InlineKeyboardButton(f"{'✅ ' if rec_val==30 else ''}30mitAntes", callback_data="alarm_val_30"),
            InlineKeyboardButton(f"{'✅ ' if rec_val==60 else ''}1horaantes", callback_data="alarm_val_60")
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
        f"Diseñado para reducir la carga mental, evitar la parálisis por análisis y mantener el foco."
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

    now = datetime.now()
    if "wizard" not in context.user_data:
        context.user_data["wizard"] = {
            "dia": str(now.day).zfill(2),
            "mes": str(now.month).zfill(2),
            "ano": str(now.year),
            "hora": "09",
            "minuto": "00",
            "ampm": "AM",
            "motivo": "Sin definir",
            "mostrar_alarma": False,
            "rec_val": 15,
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
            await query.answer("⛔ Acceso denegado.", show_alert=True)
            return
        await query.edit_message_text("🛡️ Panel de Administración:", reply_markup=teclado_admin())

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
            cur.close()
            conn.close()
            msg = f"📊 Estadísticas:\n• Usuarios: {total_usuarios}\n• Tareas: {total_tareas}"
            kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_admin")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}", reply_markup=teclado_admin())

    elif query.data == "admin_listar_otros":
        if not es_admin:
            return
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT id, chat_id, motivo, estado FROM tareas ORDER BY creado_en DESC LIMIT 15;")
            tareas = cur.fetchall()
            cur.close()
            conn.close()
            msg = "👥 Tareas globales:\n" + ("\n".join([f"• #{t[0]} [U:{t[1]}] {t[2]} ({t[3]})" for t in tareas]) if tareas else "Vacío")
            kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_admin")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}", reply_markup=teclado_admin())

    # --- MENÚ DE PRUEBAS DE ALARMA (PLAN TEXT ROBUST) ---
    elif query.data == "admin_pruebas":
        if not es_admin:
            return
        kb = [
            [InlineKeyboardButton("⚡ Alarma Inmediata", callback_data="test_alarm_now")],
            [InlineKeyboardButton("⏱️ Alarma en 5 Minutos", callback_data="test_alarm_5m")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="menu_admin")]
        ]
        await query.edit_message_text(
            "🧪 Menú de Pruebas de Alarma\n\n"
            "Nota sobre el teléfono: En Telegram, las alarmas llegan como mensajes push con sonido/vibración. "
            "Asegúrate de que las notificaciones de la app de Telegram y tu chat con este bot no estén silenciadas.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif query.data == "test_alarm_now":
        if not es_admin:
            return
        try:
            # Enviamos un mensaje nuevo Y un popup de alerta nativo para asegurar feedback inmediato
            await context.bot.send_message(
                chat_id=chat_id,
                text="🚨🔔 [PRUEBA] ¡ALARMA INMEDIATA ACTIVADA! 📱💥\n\n"
                     "Si ves esto en tu móvil, el sistema de avisos funciona correctamente. ⚡"
            )
            await query.answer("✅ ¡Alarma inmediata enviada con éxito al chat!", show_alert=True)
        except Exception as e:
            await query.answer(f"❌ Error: {e}", show_alert=True)

    elif query.data == "test_alarm_5m":
        if not es_admin:
            return
        await query.answer("⏱️ Alarma programada para dentro de 5 minutos.", show_alert=True)
        
        async def tarea_alarma_5m():
            await asyncio.sleep(300) # 5 minutos
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🚨🔔 [PRUEBA] ¡ALARMA DE LOS 5 MINUTOS! 📱💥\n\n"
                         "El temporizador asíncrono ha saltado perfectamente en tu móvil. ⚡"
                )
            except Exception as e:
                logger.error(f"Error en prueba 5m: {e}")
        
        asyncio.create_task(tarea_alarma_5m())

    # --- MENÚ ABOUT (PLAIN TEXT ROBUST) ---
    elif query.data == "m8_about":
        about_text = (
            "ℹ️ Acerca de Tareas_TDAH (About)\n\n"
            "👨‍💻 Creador: Abel Carmona Gracia\n"
            "🌐 GitHub: https://github.com/abelcarmona\n"
            "🧠 Propósito: Bot de automatización y gestión anti-parálisis diseñado para simplificar la carga mental y potenciar el hiperfoco.\n\n"
            "💬 \"El TDAH es la evolución, disfrútalo\" ⚡"
        )
        kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")]]
        await query.edit_message_text(about_text, reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "admin_limpiar_db":
        if not es_admin:
            return
        ejecutar_rutina_limpieza_bd()
        kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_admin")]]
        await query.edit_message_text("🧹 Limpieza ejecutada.", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "m1_crear":
        await query.edit_message_text("➕ Configurar Nueva Tarea:", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_dia":
        dias = list(range(1, 32))
        kb = [
            [InlineKeyboardButton(str(d).zfill(2), callback_data=f"select_dia_{d}") for d in dias[i:i+7]]
            for i in range(0, len(dias), 7)
        ]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="m1_crear")])
        await query.edit_message_text("Selecciona el Día:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("select_dia_"):
        wdata["dia"] = str(query.data.split("_")[2]).zfill(2)
        await query.edit_message_text("➕ Configurar Nueva Tarea:", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_mes":
        kb = [[InlineKeyboardButton(str(m).zfill(2), callback_data=f"select_mes_{m}") for m in range(r, r+4)] for r in range(1, 13, 4)]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="m1_crear")])
        await query.edit_message_text("Selecciona el Mes:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("select_mes_"):
        wdata["mes"] = str(query.data.split("_")[2]).zfill(2)
        await query.edit_message_text("➕ Configurar Nueva Tarea:", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_ano":
        a_actual = datetime.now().year
        kb = [[InlineKeyboardButton(str(a), callback_data=f"select_ano_{a}") for a in range(a_actual, a_actual+3)]]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="m1_crear")])
        await query.edit_message_text("Selecciona el Año:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("select_ano_"):
        wdata["ano"] = str(query.data.split("_")[2])
        await query.edit_message_text("➕ Configurar Nueva Tarea:", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_hora_menu":
        kb = [
            [InlineKeyboardButton(f"{'✅ ' if wdata.get('hora')==str(h).zfill(2) else ''}{h}", callback_data=f"sel_h_{h}") for h in range(1, 7)],
            [InlineKeyboardButton(f"{'✅ ' if wdata.get('hora')==str(h).zfill(2) else ''}{h}", callback_data=f"sel_h_{h}") for h in range(7, 13)],
            [
                InlineKeyboardButton(f"{'✅ [AM]' if wdata.get('ampm')=='AM' else 'AM'}", callback_data="sel_ampm_AM"),
                InlineKeyboardButton(f"{'✅ [PM]' if wdata.get('ampm')=='PM' else 'PM'}", callback_data="sel_ampm_PM")
            ],
            [InlineKeyboardButton("⏱️ Siguiente: Elegir Minutos ➡️", callback_data="go_to_minutes")],
            [InlineKeyboardButton("⬅️ Volver al Asistente", callback_data="m1_crear")]
        ]
        await query.edit_message_text(f"🕒 Selecciona la Hora (1-12) y AM/PM:\nActual: {wdata.get('hora')}:{wdata.get('minuto')} {wdata.get('ampm')}", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data == "go_to_minutes":
        mins = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
        kb = [
            [InlineKeyboardButton(f"{'✅ ' if wdata.get('minuto')==str(m).zfill(2) else ''}{str(m).zfill(2)}", callback_data=f"sel_m_{m}") for m in mins[i:i+4]]
            for i in range(0, len(mins), 4)
        ]
        kb.append([InlineKeyboardButton("⬅️ Volver a Horas", callback_data="w_set_hora_menu")])
        await query.edit_message_text("⏱️ Selecciona los Minutos (intervalos de 5 min):", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("sel_h_"):
        h_val = str(query.data.split("_")[2]).zfill(2)
        wdata["hora"] = h_val
        mins = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
        kb = [
            [InlineKeyboardButton(f"{'✅ ' if wdata.get('minuto')==str(m).zfill(2) else ''}{str(m).zfill(2)}", callback_data=f"sel_m_{m}") for m in mins[i:i+4]]
            for i in range(0, len(mins), 4)
        ]
        kb.append([InlineKeyboardButton("⬅️ Volver a Horas", callback_data="w_set_hora_menu")])
        await query.edit_message_text("⏱️ Selecciona los Minutos (intervalos de 5 min):", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("sel_ampm_"):
        wdata["ampm"] = query.data.split("_")[2]
        kb = [
            [InlineKeyboardButton(f"{'✅ ' if wdata.get('hora')==str(h).zfill(2) else ''}{h}", callback_data=f"sel_h_{h}") for h in range(1, 7)],
            [InlineKeyboardButton(f"{'✅ ' if wdata.get('hora')==str(h).zfill(2) else ''}{h}", callback_data=f"sel_h_{h}") for h in range(7, 13)],
            [
                InlineKeyboardButton(f"{'✅ [AM]' if wdata.get('ampm')=='AM' else 'AM'}", callback_data="sel_ampm_AM"),
                InlineKeyboardButton(f"{'✅ [PM]' if wdata.get('ampm')=='PM' else 'PM'}", callback_data="sel_ampm_PM")
            ],
            [InlineKeyboardButton("⏱️ Siguiente: Elegir Minutos ➡️", callback_data="go_to_minutes")],
            [InlineKeyboardButton("⬅️ Volver al Asistente", callback_data="m1_crear")]
        ]
        await query.edit_message_text(f"🕒 Selecciona la Hora (1-12) y AM/PM:\nActual: {wdata.get('hora')}:{wdata.get('minuto')} {wdata.get('ampm')}", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("sel_m_"):
        m_val = str(query.data.split("_")[2]).zfill(2)
        wdata["minuto"] = m_val
        await query.edit_message_text("➕ Configurar Nueva Tarea:", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_set_motivo":
        USER_STATES[chat_id] = "ESCRIBIENDO_MOTIVO"
        await context.bot.send_message(chat_id=chat_id, text="✍️ Escribe en un mensaje el motivo o nombre de la tarea:")

    elif query.data == "w_toggle_alarma":
        wdata["mostrar_alarma"] = not wdata.get("mostrar_alarma", False)
        await query.edit_message_text("➕ Configurar Nueva Tarea:", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data.startswith("alarm_val_"):
        val = int(query.data.split("_")[2])
        wdata["rec_val"] = val
        if val == 0:
            wdata["rec_num"] = None
            wdata["rec_uni"] = None
        elif val == 15:
            wdata["rec_num"] = 15
            wdata["rec_uni"] = "minutos"
        elif val == 30:
            wdata["rec_num"] = 30
            wdata["rec_uni"] = "minutos"
        elif val == 60:
            wdata["rec_num"] = 1
            wdata["rec_uni"] = "horas"
        await query.edit_message_text("➕ Configurar Nueva Tarea:", reply_markup=teclado_wizard_fecha(wdata))

    elif query.data == "w_guardar_tarea":
        try:
            dia = int(wdata['dia'])
            mes = int(wdata['mes'])
            ano = int(wdata['ano'])
            hora_12 = int(wdata['hora'])
            minuto = int(wdata['minuto'])
            ampm = wdata['ampm']

            if ampm == "PM" and hora_12 < 12:
                hora_24 = hora_12 + 12
            elif ampm == "AM" and hora_12 == 12:
                hora_24 = 0
            else:
                hora_24 = hora_12

            max_dias = calendar.monthrange(ano, mes)[1]
            if dia > max_dias:
                await query.edit_message_text(f"❌ El mes {mes}/{ano} solo tiene {max_dias} días.", reply_markup=teclado_wizard_fecha(wdata))
                return

            fecha_str = f"{ano}-{mes:02d}-{dia:02d} {hora_24:02d}:{minuto:02d}:00"
            fecha_dt = datetime.strptime(fecha_str, "%Y-%m-%d %H:%M:%S")
            
            rec_val = wdata.get("rec_num") if wdata.get("mostrar_alarma") and wdata.get("rec_val", 15) > 0 else None
            rec_uni = wdata.get("rec_uni") if wdata.get("mostrar_alarma") and wdata.get("rec_val", 15) > 0 else None

            motivo_final = wdata.get("motivo", "Sin definir")
            if motivo_final == "Sin definir":
                await context.bot.send_message(chat_id=chat_id, text="⚠️ Asigna un motivo a la tarea.")
                return

            tid = db_guardar_tarea(chat_id, motivo_final, fecha_dt, rec_val, rec_uni)
            if tid:
                msg_confirm = (
                    f"✅ ¡Tarea Guardada con Éxito!\n\n"
                    f"📌 Motivo: {motivo_final}\n"
                    f"📅 Fecha y Hora: {dia:02d}/{mes:02d}/{ano} a las {hora_24:02d}:{minuto:02d} horas\n"
                )
                if rec_val:
                    msg_confirm += f"⏰ Alarma móvil configurada."
                
                context.user_data["wizard"] = {}
                await query.edit_message_text(msg_confirm, reply_markup=teclado_principal(es_admin))
            else:
                await query.edit_message_text("❌ Error guardando en Supabase.", reply_markup=teclado_principal(es_admin))
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}", reply_markup=teclado_principal(es_admin))

    elif query.data == "w_generar_ical":
        try:
            h_12 = int(wdata['hora'])
            m_val = int(wdata['minuto'])
            if wdata['ampm'] == "PM" and h_12 < 12:
                h_24 = h_12 + 12
            elif wdata['ampm'] == "AM" and h_12 == 12:
                h_24 = 0
            else:
                h_24 = h_12
            fecha_str = f"{wdata['ano']}{wdata['mes']}{wdata['dia']}T{h_24:02d}{m_val:02d}00Z"
            gcal_link = f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={requests.utils.quote(wdata['motivo'])}&dates={fecha_str}/{fecha_str}"
            await context.bot.send_message(chat_id=chat_id, text=f"📅 Añadir a Google Calendar:\n{gcal_link}")
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error: {e}")

    elif query.data.startswith("m2_") or query.data == "m2_listar":
        filtro = "todos"
        if "_" in query.data and query.data != "m2_listar":
            filtro = query.data.split("_")[1]

        tareas = db_obtener_tareas(chat_id, filtro)
        kb_filtros = [
            [
                InlineKeyboardButton(f"{'✅ ' if filtro=='dia' else ''}Hoy", callback_data="m2_dia"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='semana' else ''}Semana", callback_data="m2_semana"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='mes' else ''}Mes", callback_data="m2_mes"),
                InlineKeyboardButton(f"{'✅ ' if filtro=='todos' else ''}Todas", callback_data="m2_todos")
            ],
            [InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")]
        ]

        meses_es = {
            1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
            7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
        }

        msg = f"📋 Tareas ({filtro.upper()}):\n\n"
        if tareas:
            if filtro == "semana":
                grupos = {}
                for t in tareas:
                    sem = t[2].isocalendar()[1]
                    grupos.setdefault(sem, []).append(t)
                for sem, lista in sorted(grupos.items()):
                    msg += f"--- Semana {sem} ---\n"
                    for t in lista:
                        sec_txt = " [🔄 Secuencial]" if t[6] else ""
                        msg += f"• #{t[0]} [{t[2].strftime('%d/%m %H:%M')}] {t[1]}{sec_txt}\n"
                    msg += "\n"
            elif filtro == "mes":
                grupos = {}
                for t in tareas:
                    m_num = t[2].month
                    grupos.setdefault(m_num, []).append(t)
                for m_num, lista in sorted(grupos.items()):
                    nombre_mes = meses_es.get(m_num, str(m_num))
                    msg += f"--- {nombre_mes} ---\n"
                    for t in lista:
                        sec_txt = " [🔄 Secuencial]" if t[6] else ""
                        msg += f"• #{t[0]} [{t[2].strftime('%d/%m %H:%M')}] {t[1]}{sec_txt}\n"
                    msg += "\n"
            else:
                for t in tareas:
                    sec_txt = " [🔄 Secuencial]" if t[6] else ""
                    msg += f"• #{t[0]} [{t[2].strftime('%d/%m %H:%M')}] {t[1]}{sec_txt}\n"
        else:
            msg += "🎉 Sin tareas pendientes."

        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb_filtros))

    elif query.data == "m3_alarmas":
        tareas = db_obtener_tareas(chat_id, "todos")
        tareas_con_alarma = [t for t in tareas if t[3] is not None]
        
        msg = "⏰ Alarmas asociadas a tus tareas pendientes:\n\n"
        if tareas_con_alarma:
            for t in tareas_con_alarma:
                tid, motivo, f_venc, rec_val, rec_uni = t[0], t[1], t[2], t[3], t[4]
                if rec_uni == "minutos":
                    alarma_desc = f"{rec_val} minutos antes"
                elif rec_uni == "horas":
                    alarma_desc = f"{rec_val} hora(s) antes"
                else:
                    alarma_desc = f"{rec_val} {rec_uni}"
                
                msg += f"📌 #{tid} {motivo}\n   📅 Vence: {f_venc.strftime('%d/%m/%Y %H:%M')}\n   ⏰ Alarma: {alarma_desc}\n\n"
        else:
            msg += "🎉 No tienes ninguna tarea con alarma configurada."

        kb = [[InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("m4_") or query.data == "m4_modificar":
        tareas = db_obtener_tareas(chat_id, "todos")
        kb = []
        msg = "✏️ Modificar, Completar o Poner Alarma a Tareas:\n\n" + ("Selecciona una acción:" if tareas else "✨ No hay tareas pendientes.")
        for t in tareas:
            sec_txt = " [🔄 Secuencial]" if t[6] else ""
            msg += f"📌 #{t[0]} [{t[2].strftime('%d/%m %H:%M')}] {t[1]}{sec_txt}\n"
            kb.append([
                InlineKeyboardButton(f"✅ #{t[0]} Hecha", callback_data=f"mod_comp_{t[0]}"),
                InlineKeyboardButton(f"⏰ #{t[0]} Alarma", callback_data=f"mod_alarm_menu_{t[0]}"),
                InlineKeyboardButton(f"🗑️ #{t[0]} Borrar", callback_data=f"mod_elim_{t[0]}")
            ])
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")])
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("mod_alarm_menu_"):
        tid = int(query.data.split("_")[3])
        kb = [
            [
                InlineKeyboardButton("En hora", callback_data=f"set_t_alarm_{tid}_0"),
                InlineKeyboardButton("15MntAntes", callback_data=f"set_t_alarm_{tid}_15")
            ],
            [
                InlineKeyboardButton("30mitAntes", callback_data=f"set_t_alarm_{tid}_30"),
                InlineKeyboardButton("1horaantes", callback_data=f"set_t_alarm_{tid}_60")
            ],
            [InlineKeyboardButton("⬅️ Volver a Modificar", callback_data="m4_modificar")]
        ]
        await query.edit_message_text(f"⏰ Selecciona la Alarma Móvil para la Tarea #{tid}:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("set_t_alarm_"):
        parts = query.data.split("_")
        tid = int(parts[3])
        val = int(parts[4])
        
        if val == 0:
            db_actualizar_alarma_tarea(tid, chat_id, None, None)
        elif val == 15:
            db_actualizar_alarma_tarea(tid, chat_id, 15, "minutos")
        elif val == 30:
            db_actualizar_alarma_tarea(tid, chat_id, 30, "minutos")
        elif val == 60:
            db_actualizar_alarma_tarea(tid, chat_id, 1, "horas")

        await context.bot.send_message(chat_id=chat_id, text=f"✅ ¡Alarma configurada con éxito para la Tarea #{tid}!")
        try:
            await query.edit_message_text("✏️ Modificar Tareas", reply_markup=teclado_principal(es_admin))
        except Exception:
            pass

    elif query.data.startswith("mod_comp_"):
        tid = int(query.data.split("_")[2])
        db_completar_tarea(tid, chat_id)
        await context.bot.send_message(chat_id=chat_id, text=f"🎉 ¡Tarea #{tid} completada! Dopamina liberada ⚡")
        try:
            await query.edit_message_text("✏️ Modificar Tareas", reply_markup=teclado_principal(es_admin))
        except Exception:
            pass

    elif query.data.startswith("mod_elim_"):
        tid = int(query.data.split("_")[2])
        db_eliminar_tarea(tid, chat_id)
        await context.bot.send_message(chat_id=chat_id, text=f"🗑️ Tarea #{tid} eliminada.")
        try:
            await query.edit_message_text("✏️ Modificar Tareas", reply_markup=teclado_principal(es_admin))
        except Exception:
            pass

    elif query.data == "m5_ia_desglose":
        tareas = db_obtener_tareas(chat_id, "todos")
        if not tareas:
            await query.edit_message_text("🧩 IA Anti-Parálisis: Sin tareas pendientes.", reply_markup=teclado_principal(es_admin))
            return
        kb = [[InlineKeyboardButton(f"🧩 Desglosar: {t[1][:22]}...", callback_data=f"ia_desg_{t[0]}")] for t in tareas[:5]]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")])
        await query.edit_message_text("🧩 Elige la tarea bloqueante:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("ia_desg_"):
        tid = int(query.data.split("_")[2])
        tareas = db_obtener_tareas(chat_id, "todos")
        tarea_sel = next((t for t in tareas if t[0] == tid), None)
        if tarea_sel:
            await query.edit_message_text("🧠 Gemini fragmentando tarea en micro-pasos...")
            prompt = f"Actúa como experto TDAH. Divide la tarea '{tarea_sel[1]}' en 3 micro-pasos de menos de 5 minutos."
            res = await asyncio.to_thread(consultar_gemini, prompt)
            await context.bot.send_message(chat_id=chat_id, text=f"🧩 Micro-pasos:\n\n{res}")

    elif query.data == "m6_ia_dopamina":
        tareas = db_obtener_tareas(chat_id, "todos")
        if not tareas:
            await query.edit_message_text("⚡ Coach Dopamina: Sin tareas.", reply_markup=teclado_principal(es_admin))
            return
        resumen = "\n".join([f"- {t[1]}" for t in tareas[:7]])
        await query.edit_message_text("⚡ Analizando con Gemini...")
        prompt = f"Actúa como Coach TDAH con gamificación. Tareas:\n{resumen}\n1. Asigna XP.\n2. Elige la tarea ideal para empezar AHORA."
        res = await asyncio.to_thread(consultar_gemini, prompt)
        await context.bot.send_message(chat_id=chat_id, text=f"⚡ Coach Dopamina & Foco:\n\n{res}")

    elif query.data == "m7_secuencial":
        tareas = db_obtener_tareas(chat_id, "todos")
        if not tareas:
            await query.edit_message_text("🔄 Tareas Secuenciales: Sin tareas.", reply_markup=teclado_principal(es_admin))
            return
        kb = [[InlineKeyboardButton(f"🔄 {t[1][:25]}...", callback_data=f"sec_sel_{t[0]}")] for t in tareas[:5]]
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_principal")])
        await query.edit_message_text("🔄 Selecciona tarea para automatizar frecuencia:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("sec_sel_"):
        tid = int(query.data.split("_")[2])
        kb = [
            [InlineKeyboardButton("📆 Diaria (1 día)", callback_data=f"sec_freq_{tid}_1")],
            [InlineKeyboardButton("📅 Semanal (7 días)", callback_data=f"sec_freq_{tid}_7")],
            [InlineKeyboardButton("🗓️ Mensual (30 días)", callback_data=f"sec_freq_{tid}_30")],
            [InlineKeyboardButton("⬅️ Volver", callback_data="m7_secuencial")]
        ]
        await query.edit_message_text("Selecciona la frecuencia:", reply_markup=InlineKeyboardMarkup(kb))

    elif query.data.startswith("sec_freq_"):
        parts = query.data.split("_")
        tid = int(parts[2])
        intervalo = int(parts[3])
        db_hacer_secuencial(tid, intervalo, chat_id)
        await query.edit_message_text(f"✅ ¡Tarea #{tid} configurada como secuencia recurrente!", reply_markup=teclado_principal(es_admin))

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
        await update.message.reply_text("✅ Motivo guardado:", reply_markup=teclado_wizard_fecha(context.user_data["wizard"]))
    elif estado == "ESCRIBIENDO_ALARMA_NUM":
        if "wizard" not in context.user_data:
            context.user_data["wizard"] = {}
        text = "".join(filter(str.isdigit, update.message.text))
        context.user_data["wizard"]["rec_num"] = int(text) if text else 15
        USER_STATES.pop(chat_id, None)
        await update.message.reply_text("✅ Alarma actualizada:", reply_markup=teclado_wizard_fecha(context.user_data["wizard"]))

# --- 7. MAIN APPLICATION ---
def main():
    logger.info("Iniciando Tareas_TDAH Bot con Corrección de Markdown y Alarmas...")
    init_db()

    threading.Thread(target=start_health_check, daemon=True).start()
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    if not TOKEN:
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(procesar_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto))

    threading.Thread(target=verificar_y_enviar_alertas, args=(app,), daemon=True).start()

    app.run_polling()

if __name__ == "__main__":
    main()
