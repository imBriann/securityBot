import os
import sqlite3
import asyncio 
import httpx 
import uuid
import re 
from collections import defaultdict, deque 
from contextlib import asynccontextmanager 
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from io import BytesIO
from PIL import Image
import pytesseract
from dotenv import load_dotenv 
from datetime import datetime
import datetime  # Ensure this is at the top of the file

load_dotenv()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Estados del usuario
ESTADO_PENDIENTE_TERMINOS = 0
ESTADO_PENDIENTE_NOMBRE = 1
ESTADO_PENDIENTE_EDAD = 2
ESTADO_PENDIENTE_CONOCIMIENTO = 3
ESTADO_REGISTRADO = 4
ESTADO_ESPERANDO_RESPUESTA_PHISHING = 5 # Nuevo estado
ESTADO_ESPERANDO_MAS_DETALLES = 6 # Nuevo estado

if not all([VERIFY_TOKEN, ACCESS_TOKEN, PHONE_NUMBER_ID, DEEPSEEK_API_KEY]):
    print("ERROR CRÍTICO: Una o más variables de entorno no están configuradas.")

if os.name == "nt": 
    tesseract_path = os.getenv("TESSERACT_CMD_PATH", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
else:
    tesseract_path = "/usr/bin/tesseract"

if os.path.exists(tesseract_path):
    pytesseract.pytesseract.tesseract_cmd = tesseract_path
else:
    print(f"ADVERTENCIA: Tesseract OCR no encontrado en {tesseract_path}.")

http_client: httpx.AsyncClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    print("Iniciando aplicación y cliente HTTP...")
    http_client = httpx.AsyncClient(timeout=45.0) 
    yield 
    print("Cerrando cliente HTTP y finalizando aplicación...")
    if http_client:
        await http_client.aclose()

app = FastAPI(lifespan=lifespan) 

user_locks = defaultdict(asyncio.Lock)
DB_NAME = "usuarios_bot.db" 

PROCESSED_MESSAGE_IDS_CACHE_SIZE = 1000 
processed_message_ids = deque(maxlen=PROCESSED_MESSAGE_IDS_CACHE_SIZE)

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row 
    return conn

IMAGES_DIR = "imagenes_recibidas"
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR)

def setup_database():
    conn_setup = get_db_connection()
    cursor_setup = conn_setup.cursor()
    cursor_setup.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        telefono TEXT PRIMARY KEY,
        nombre TEXT,
        edad INTEGER,
        conocimiento TEXT,
        acepto_terminos INTEGER DEFAULT 0,
        estado INTEGER DEFAULT 0,
        mensajes_enviados INTEGER DEFAULT 0,
        last_analysis_details TEXT,
        last_image_ocr_text TEXT,
        last_image_analysis_raw TEXT,
        last_image_id_processed TEXT,
        last_image_timestamp DATETIME
    );
    """)
    cursor_setup.execute("""
    CREATE TABLE IF NOT EXISTS imagenes_procesadas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telefono_usuario TEXT,
        nombre_archivo_imagen TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (telefono_usuario) REFERENCES usuarios(telefono)
    );
    """)
    conn_setup.commit()
    conn_setup.close()

setup_database() 

def db_get_user(telefono: str) -> sqlite3.Row | None:
    conn_db = get_db_connection()
    cursor_db = conn_db.cursor()
    cursor_db.execute("SELECT * FROM usuarios WHERE telefono = ?", (telefono,))
    user = cursor_db.fetchone()
    conn_db.close()
    return user

def db_create_user(telefono: str):
    conn_db = get_db_connection()
    cursor_db = conn_db.cursor()
    try:
        cursor_db.execute("INSERT INTO usuarios (telefono, acepto_terminos, estado) VALUES (?, ?, ?)", 
                          (telefono, 0, ESTADO_PENDIENTE_TERMINOS))
        conn_db.commit()
    except sqlite3.IntegrityError:
        print(f"Intento de crear usuario duplicado: {telefono}")
    finally:
        conn_db.close()

def db_update_user(telefono: str, data: dict):
    if not data:
        print(f"DEBUG: db_update_user llamado para {telefono} sin datos. Retornando.")  # NEW LOG
        return

    fields = ", ".join([f"{key} = ?" for key in data])
    values = list(data.values())
    values.append(telefono)

    conn_db = None  # Define outside try to ensure closure in finally
    try:
        conn_db = get_db_connection()
        cursor_db = conn_db.cursor()
        query = f"UPDATE usuarios SET {fields} WHERE telefono = ?"  # NEW LOG
        print(f"DEBUG: Ejecutando SQL: {query} con valores (excepto el último que es el teléfono): {tuple(values[:-1])} para tel: {telefono}")  # NEW LOG
        cursor_db.execute(query, tuple(values))
        conn_db.commit()
        print(f"DEBUG: Commit exitoso para {telefono}.")  # NEW LOG
    except sqlite3.Error as e_sqlite:  # Capture SQLite-specific errors
        print(f"ERROR SQLITE en db_update_user para {telefono}: {e_sqlite}. Query: {query}, Values: {tuple(values)}")  # NEW LOG
        if conn_db:
            conn_db.rollback()  # Rollback changes on error
        raise  # Re-raise to propagate error
    except Exception as e_general:  # Capture other possible errors
        print(f"ERROR GENERAL en db_update_user para {telefono}: {e_general}. Query: {query}, Values: {tuple(values)}")  # NEW LOG
        if conn_db:
            conn_db.rollback()
        raise
    finally:
        if conn_db:
            conn_db.close()
            print(f"DEBUG: Conexión DB cerrada para {telefono} en db_update_user.")  # NEW LOG

def db_save_image_record(telefono_usuario: str, nombre_archivo_imagen: str):
    conn_db = get_db_connection()
    cursor_db = conn_db.cursor()
    cursor_db.execute(
        "INSERT INTO imagenes_procesadas (telefono_usuario, nombre_archivo_imagen) VALUES (?, ?)",
        (telefono_usuario, nombre_archivo_imagen)
    )
    conn_db.commit()
    conn_db.close()

async def send_whatsapp_message(to: str, text: str):
    global http_client 
    if not http_client:
        print("Error: El cliente HTTP no está inicializado.")
        return
    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print("Error: ACCESS_TOKEN o PHONE_NUMBER_ID no configurados.")
        return

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    
    try:
        response = await http_client.post(url, json=payload, headers=headers)
        response.raise_for_status() 
        print(f"Mensaje enviado a {to}: '{text[:50]}...' (Estado: {response.status_code})")
    except httpx.HTTPStatusError as e:
        print(f"Error al enviar mensaje a WhatsApp ({to}): {e.response.status_code} - {e.response.text}")
    except httpx.RequestError as e:
        print(f"Error de red al enviar mensaje a WhatsApp ({to}): {e}")
    except Exception as e:
        print(f"Error inesperado en send_whatsapp_message ({to}): {e}")

async def analyze_with_deepseek(message_text: str, mode: str, user_profile: dict = None) -> str | None:
    global http_client 
    if not http_client:
        print("Error: El cliente HTTP no está inicializado.")
        return "Lo siento, el servicio de análisis no está disponible en este momento (cliente no listo)."
    if not DEEPSEEK_API_KEY:
        print("Error: DEEPSEEK_API_KEY no configurado.")
        return "Lo siento, el servicio de análisis no está disponible en este momento."
    if user_profile is None: user_profile = {}

    user_name_for_prompt = user_profile.get('nombre', 'usuario') 

    prompts_config = {
        "nombre": {
            "system": (
                "Eres un experto en extraer nombres de personas de un texto. El usuario te dará un mensaje donde se espera que esté su nombre.\n"
                "Analiza la entrada y responde SOLO con una de estas opciones:\n"
                "- Si encuentras un nombre de persona claro y plausible, responde con: NOMBRE_VALIDO:{nombre_extraido} (ej. NOMBRE_VALIDO:Carlos, NOMBRE_VALIDO:Maria Eugenia).\n"
                "- Si el texto NO parece ser un nombre de persona (ej. 'gato', '123', 'no quiero decirlo'), responde con: NOMBRE_INVALIDO\n"
                "- Si el texto es ambiguo, muy corto, o no estás seguro si es un nombre real (ej. 'si', 'ok', 'xyz'), responde con: NOMBRE_CONFUSO\n"
                "No expliques nada más. Sé estricto con los nombres, deben parecer reales."
            ), 
            "user": message_text
        },
        "edad": {
            "system": (
                "Eres un experto en procesamiento de lenguaje natural para extraer la edad de una persona de un texto. El usuario te dará un mensaje donde se espera que indique su edad.\n"
                "La edad puede venir como número ('35'), con palabras ('sesenta años', 'tengo cuarenta y dos'), o de forma más informal.\n"
                "Analiza la entrada y responde SOLO con una de estas opciones:\n"
                "- Si puedes extraer un número de edad plausible (entre 5 y 120 años), responde con: EDAD_VALIDA:{numero_edad} (ej. EDAD_VALIDA:65, EDAD_VALIDA:30).\n"
                "- Si el texto claramente indica que no es una edad o es basura (ej. 'gato', 'no sé', 'ayer comí pollo'), responde con: EDAD_INVALIDA\n"
                "- Si el texto es ambiguo, no estás seguro de poder extraer un número de edad correcto, o parece una respuesta evasiva (ej. 'unos cuantos', 'joven', 'prefiero no decir'), responde con: EDAD_NO_CLARA\n"
                "No expliques nada más. Intenta ser flexible con la forma en que se expresa la edad, pero asegúrate de que el número sea razonable."
            ), 
            "user": message_text
        },
        "conocimiento": {
            "system": (
                "Clasifica el siguiente texto SOLO como una de estas opciones: 'Sí', 'No', 'Poco' o 'CONOCIMIENTO_AMBIGUO'.\n"
                "El usuario está respondiendo a la pregunta '¿qué tanto sabes sobre ciberseguridad y estafas en línea?'.\n"
                "- 'Sí': si dice que sabe, tiene experiencia, entiende bien, etc.\n"
                "- 'No': si dice que no sabe, no entiende, es nuevo en esto, etc.\n"
                "- 'Poco': si dice que sabe un poquito, más o menos, algo, regular, etc.\n"
                "- 'CONOCIMIENTO_AMBIGUO': si la respuesta es muy vaga, evasiva, una pregunta como 'qué?' o 'no entiendo la pregunta', o no se puede clasificar claramente en las anteriores (ej. 'depende', 'a veces', 'gracias'). Ten especial cuidado con respuestas cortas que no sean claramente afirmativas o negativas sobre su conocimiento.\n"
                "No expliques nada más. Solo una de las cuatro opciones."
            ), 
            "user": message_text
        },
        "intencion": { # Este prompt se usa cuando el usuario está en ESTADO_REGISTRADO (4)
            "system": (
                "Eres un asistente inteligente para WhatsApp. Tu tarea es analizar el siguiente mensaje de un usuario y determinar su intención principal. "
                "El usuario ya está registrado y podría estar saludando, pidiendo analizar algo, o haciendo una pregunta.\n" # Eliminada la parte de responder a pregunta anterior
                "Responde SOLO con una de estas opciones (una sola palabra, en minúsculas y sin explicaciones adicionales):\n"
                "- saludo: si el mensaje es un saludo o una interacción social simple (ej: hola, buenos días, ¿cómo estás?, gracias, ok).\n"
                "- analizar: si el usuario quiere que analices un mensaje de texto, el contenido de una imagen, o cualquier cosa que le parezca sospechosa de ser una estafa, phishing, fraude, o que contenga información engañosa. Esto incluye mensajes que el usuario podría haber copiado y pegado, incluso si no lo pide explícitamente pero el contenido del mensaje sugiere que es para revisión.\n"
                "- pregunta: si el usuario está haciendo una pregunta específica sobre ciberseguridad, cómo protegerse, qué es un tipo de estafa, etc. (que no sea simplemente reenviar un mensaje para analizar).\n"
                "- irrelevante: si el mensaje no tiene relación con los temas anteriores (ej: preguntas sobre el clima, deportes, chistes no relacionados, etc.).\n\n"
                "Prioriza 'analizar' si el texto del mensaje parece ser el contenido de un mensaje sospechoso que el usuario quiere verificar, incluso si no lo pide explícitamente."
            ), 
            "user": message_text
        },
        "phishing": { # Prompt para analizar si un mensaje es phishing
            "system": (
                f"Eres SecurityBot-WA, un asistente de seguridad digital en Colombia, muy AMABLE, EMPÁTICO y CLARO. Te diriges al usuario {user_name_for_prompt}.\n"
                "Tu misión es revisar el siguiente mensaje y determinar si parece una estafa digital (phishing, smishing, etc.). Luego, crea una respuesta adaptada al perfil del usuario.\n\n"
                f"PERFIL DEL USUARIO ACTUAL: Nombre: {user_name_for_prompt}, Edad: {user_profile.get('edad', 'Desconocida')}, Nivel de conocimiento en ciberseguridad: {user_profile.get('conocimiento', 'Desconocido')}.\n\n"
                "INSTRUCCIONES DE TONO Y LENGUAJE:\n"
                f"- Siempre dirígete al usuario por su nombre ({user_name_for_prompt}) de forma natural al inicio o cuando sea apropiado.\n"
                "- Usa un tono cálido, paciente y tranquilizador. Muchos usuarios pueden estar preocupados o no entender bien estos temas.\n"
                "- Utiliza emojis con moderación para añadir claridad y amabilidad (ej: ✅, ⚠️, 🤔, 🛡️, 👍, 😊).\n"
                f"- Si {user_name_for_prompt} es un adulto mayor (60+ años) o su conocimiento es 'No': Explica las cosas como si hablaras con un familiar querido, con mucha paciencia. Usa frases cortas, lenguaje MUY sencillo, ejemplos cotidianos. Evita TOTALMENTE la jerga técnica. Sé muy paso a paso.\n"
                f"- Si el conocimiento de {user_name_for_prompt} es 'Poco': Usa un lenguaje claro, intermedio, con ejemplos sencillos. Evita tecnicismos innecesarios.\n"
                f"- Si el conocimiento de {user_name_for_prompt} es 'Sí': Puedes ser un poco más directo y usar algún término técnico si es relevante, pero siempre prioriza la claridad y un tono amable y respetuoso.\n\n"
                "**NOTA ESPECIAL SOBRE TEXTO DE IMÁGENES (OCR)**: El mensaje que vas a analizar podría provenir de una imagen y haber sido transcrito por un sistema OCR. Esto significa que PUEDE CONTENER ERRORES, letras o palabras extrañas, o texto mal formado. Por favor, TEN MUCHA PACIENCIA con estos errores e INTENTA INTERPRETAR LA INTENCIÓN Y EL CONTENIDO PRINCIPAL del texto original a pesar de las posibles imperfecciones de la transcripción antes de realizar tu análisis de seguridad. No te enfoques en los errores de OCR, sino en el mensaje subyacente que {user_name_for_prompt} quiso compartir.\n\n"
                "INSTRUCCIONES PARA LA RESPUESTA:\n"
                "Tu respuesta DEBE estar estructurada en dos partes, separadas por la cadena '---DETALLES_SIGUEN---'.\n"
                "PARTE 1 (Resumen Breve): Antes del separador '---DETALLES_SIGUEN---', proporciona un resumen MUY BREVE y directo (1-2 frases) sobre el mensaje analizado. Indica el riesgo principal (ej: 'Parece una estafa de tipo suplantación de identidad.' o 'En principio, este mensaje no parece ser una estafa.'). NO DES NINGUNA EXPLICACIÓN DETALLADA AQUÍ. Finaliza OBLIGATORIAMENTE esta primera parte con la frase: 'Si quieres el análisis completo y mis recomendaciones, responde SÍ_DETALLES.'\n"
                "PARTE 2 (Análisis Completo): Después del separador '---DETALLES_SIGUEN---', incluye el análisis completo y detallado, manteniendo la siguiente estructura OBLIGATORIA:\n"
                "🔍 *Análisis del mensaje recibido*\n"
                "✅ *Resultado*: (Sí, parece una estafa / No, no parece una estafa / No estoy seguro, pero te doy recomendaciones)\n"
                "⚠️ *Tipo de estafa*: (Phishing, Smishing, etc. o 'No aplica si no es estafa')\n"
                "📌 *Mi opinión detallada*: (Explicación POR QUÉ llegaste a esa conclusión, adaptando la explicación al perfil de {user_name_for_prompt}. Señala las pistas o elementos sospechosos, o por qué no parece peligroso).\n"
                "🧠 *¿Cómo suelen funcionar estos engaños?* (Si es estafa, explica brevemente el mecanismo de forma sencilla y adaptada al perfil. Si no es estafa, puedes omitir esta parte o dar un consejo general breve).\n"
                "🛡️ *Mis recomendaciones para ti, {user_name_for_prompt}*: (Consejos CLAROS, ÚTILES y FÁCILES de seguir. Si es estafa, qué hacer ahora. Si no lo es, cómo mantenerse alerta en general).\n\n"
                "IMPORTANTE (para la PARTE 2):\n"
                "- Si el análisis concluye que ES UNA ESTAFA (o altamente sospechoso), DEBES terminar tu respuesta (la PARTE 2) preguntando de forma amable: '{user_name_for_prompt}, ¿llegaste a hacer clic en algún enlace de ese mensaje, descargaste algo o compartiste información personal? Puedes responderme SÍ o NO. Si necesitas ayuda más específica sobre qué hacer si interactuaste, escribe AYUDA. ¡Estoy aquí para apoyarte! 😊'\n"
                "- Si NO ES UNA ESTAFA, finaliza la PARTE 2 con un mensaje positivo y de prevención general.\n"
            ),
            "user": f"Por favor, {user_name_for_prompt} me envió este mensaje para analizarlo: \"{message_text}\""
        },
        "ayuda_post_estafa": { # Prompt para cuando el usuario pide AYUDA o dice SÍ interactuó
            "system": (
                f"Eres SecurityBot-WA, un asistente de seguridad digital en Colombia, muy AMABLE, EMPÁTICO y CLARO. Te diriges al usuario {user_name_for_prompt}.\n"
                f"{user_name_for_prompt} ha indicado que PUDO haber interactuado con una estafa (o ha pedido ayuda directamente) y necesita pasos específicos.\n"
                f"PERFIL DEL USUARIO ACTUAL: Nombre: {user_name_for_prompt}, Edad: {user_profile.get('edad', 'Desconocida')}, Nivel de conocimiento en ciberseguridad: {user_profile.get('conocimiento', 'Desconocido')}.\n\n"
                "INSTRUCCIONES DE TONO Y LENGUAJE:\n"
                "- Mantén la calma y transmite tranquilidad a {user_name_for_prompt}. Asegúrale que le ayudarás a tomar los siguientes pasos.\n"
                "- Usa un lenguaje adaptado a su perfil (edad y conocimiento), similar a las instrucciones del modo 'phishing'.\n"
                "- Proporciona pasos CLAROS, CONCISOS y ACCIONABLES que debe seguir INMEDIATAMENTE. Organiza la respuesta en pasos numerados (1️⃣, 2️⃣, 3️⃣...) o con viñetas claras (🔹) para fácil lectura.\n"
                "- Usa emojis con moderación para guiar y tranquilizar (ej. 🆘, 🛡️, 🔑, 🏦, 💻).\n\n"
                "QUÉ CUBRIR (adapta según lo que sea más relevante y comprensible para {user_name_for_prompt}):\n"
                "1.  **No entrar en pánico:** Es el primer paso. 'Respira profundo, {user_name_for_prompt}, vamos a ver esto juntos.'\n"
                "2.  **Contraseñas:** 'Lo primero y más importante: cambia tus contraseñas INMEDIATAMENTE. Especialmente la de tu correo electrónico principal, tus bancos y redes sociales. Intenta que sean fuertes y diferentes para cada sitio.'\n"
                "3.  **Bancos/Finanzas:** 'Si crees que compartiste datos de tu banco o tarjetas, llama YA MISMO a tu banco. Ellos te dirán cómo bloquear tus tarjetas o revisar si hay movimientos raros.'\n"
                "4.  **Actividad Sospechosa:** 'Revisa con calma los últimos movimientos de tus cuentas bancarias y tu correo electrónico por si ves algo que no reconozcas.'\n"
                "5.  **Autenticación de Dos Factores (2FA):** 'Una capa extra de seguridad muy buena es la \"verificación en dos pasos\" o 2FA. Si puedes, actívala en todas tus cuentas importantes (como WhatsApp, correo, bancos).'\n"
                "6.  **Dispositivos:** 'Si descargaste algún archivo del mensaje sospechoso, sería bueno pasarle un antivirus a tu teléfono o computador.'\n"
                "7.  **Reportar (Opcional, pero recomendado):** 'En Colombia, puedes reportar estos fraudes en el CAI Virtual de la Policía Nacional. Esto ayuda a que otros no caigan.'\n"
                "8.  **No seguir interactuando:** 'Muy importante: no respondas más a ese mensaje o a quien te lo envió.'\n"
                "9.  **Aprender del incidente:** 'Recuerda, {user_name_for_prompt}, siempre es mejor desconfiar un poquito de mensajes inesperados que piden información o te apuran.'\n\n"
                f"Finaliza con un mensaje de apoyo, como: 'Sé que esto puede ser preocupante, {user_name_for_prompt}, pero actuando rápido puedes protegerte mucho mejor. ¡No dudes en consultarme si tienes más preguntas o necesitas que te repita algo! Estoy aquí para ayudarte. 💪'"
            ), 
            "user": f"{user_name_for_prompt} necesita ayuda específica tras interactuar con una posible estafa (o pidió AYUDA directamente). ¿Qué pasos concretos y amables debe seguir?" 
        },
        "cyber_pregunta": { # Prompt para responder preguntas generales de ciberseguridad
            "system": (
                f"Eres SecurityBot-WA, un experto en ciberseguridad y fraudes digitales en Colombia, muy AMABLE, EDUCATIVO y PACIENTE. Te diriges al usuario {user_name_for_prompt}.\n"
                f"PERFIL DEL USUARIO ACTUAL: Nombre: {user_name_for_prompt}, Edad: {user_profile.get('edad', 'Desconocida')}, Nivel de conocimiento en ciberseguridad: {user_profile.get('conocimiento', 'Desconocido')}.\n\n"
                "INSTRUCCIONES DE TONO Y LENGUAJE:\n"
                f"- Dirígete a {user_name_for_prompt} por su nombre de forma natural.\n"
                "- Adapta tu lenguaje a su perfil (edad y conocimiento), similar a las instrucciones del modo 'phishing'. Explica conceptos complejos de forma sencilla.\n"
                "- Usa un tono positivo y alentador. El objetivo es educar y empoderar.\n"
                "- Usa emojis con moderación para hacer la explicación más amena (ej. 💡, 🛡️, 🤔, 👍, 😊).\n\n"
                "**NOTA ESPECIAL SOBRE TEXTO DE IMÁGENES (OCR)**: La pregunta de {user_name_for_prompt} podría provenir de una imagen y haber sido transcrita por un sistema OCR. Esto significa que PUEDE CONTENER ERRORES. Intenta inferir la pregunta real del usuario a pesar de las imperfecciones antes de responder.\n\n"
                "ESTRUCTURA DE LA RESPUESTA:\n"
                f"1.  Empieza con un saludo amable y reconociendo su pregunta, ej: '¡Hola, {user_name_for_prompt}! Claro, con gusto te explico sobre [tema de la pregunta]. 😊'\n"
                "2.  Explica el concepto o responde la pregunta de forma clara, concisa y adaptada.\n"
                "3.  Si es apropiado, da ejemplos sencillos o analogías.\n"
                "4.  Ofrece 1-2 consejos prácticos relacionados con la pregunta.\n"
                f"5.  Finaliza invitando a {user_name_for_prompt} a hacer más preguntas si las tiene: 'Espero que esto te sea útil, {user_name_for_prompt}. ¡Si tienes más dudas, no dudes en preguntar! 🛡️'"
            ), 
            "user": f"{user_name_for_prompt} tiene la siguiente pregunta sobre ciberseguridad: \"{message_text}\""
        }
    }
    if mode not in prompts_config: 
        print(f"Modo de análisis no reconocido: {mode}")
        return "Error interno: modo de análisis no válido."

    current_prompt = prompts_config[mode]
    payload = {"model": "deepseek-chat", "messages": [{"role": "system", "content": current_prompt["system"]}, {"role": "user", "content": current_prompt["user"]}], "temperature": 0.4, "max_tokens": 1600} 
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}

    try:
        response = await http_client.post(DEEPSEEK_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        api_response = response.json()
        if api_response.get("choices") and api_response["choices"][0].get("message"):
            return api_response["choices"][0]["message"]["content"].strip()
        print(f"Respuesta inesperada de DeepSeek API: {api_response}")
        return "No se pudo obtener una respuesta del servicio de análisis."
    except httpx.HTTPStatusError as e:
        print(f"Error de API DeepSeek ({mode}): {e.response.status_code} - {e.response.text}")
        return "Hubo un problema al contactar el servicio de análisis."
    except httpx.RequestError as e: print(f"Error de red con DeepSeek API ({mode}): {e}"); return "Problema de conexión con el servicio de análisis."
    except Exception as e: print(f"Error inesperado en analyze_with_deepseek ({mode}): {e}"); return "Lo siento, ocurrió un error inesperado."

async def download_image_from_whatsapp(media_id: str) -> bytes | None:
    global http_client 
    if not http_client: print("Error: El cliente HTTP no está inicializado."); return None
    if not ACCESS_TOKEN: print("Error: ACCESS_TOKEN no configurado."); return None
    
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        media_info_url = f"https://graph.facebook.com/v18.0/{media_id}"
        media_info_response = await http_client.get(media_info_url, headers=headers)
        media_info_response.raise_for_status()
        image_download_url = media_info_response.json()["url"]
        image_response = await http_client.get(image_download_url, headers=headers) 
        image_response.raise_for_status()
        return image_response.content
    except httpx.HTTPStatusError as e: print(f"Error HTTP al descargar imagen (media_id: {media_id}): {e.response.status_code} - {e.response.text}")
    except httpx.RequestError as e: print(f"Error de red al descargar imagen (media_id: {media_id}): {e}")
    except Exception as e: print(f"Error inesperado en download_image_from_whatsapp (media_id: {media_id}): {e}")
    return None

async def process_incoming_image_task(telefono: str, user_data: sqlite3.Row, image_id_whatsapp: str):
    user_name_for_ocr_task = user_data["nombre"] if user_data and user_data["nombre"] else "tú"
    print(f"Iniciando tarea de procesamiento de imagen para {telefono} ({user_name_for_ocr_task}), image_id_whatsapp: {image_id_whatsapp}")

    image_bytes = await download_image_from_whatsapp(image_id_whatsapp)
    if not image_bytes:
        await send_whatsapp_message(telefono, f"⚠️ Lo siento, {user_name_for_ocr_task}, no pude descargar la imagen que enviaste. ¿Podrías intentar enviarla de nuevo o verificar que sea válida? Por favor.")
        return

    try:
        image_file_name_for_db = f"{telefono}_{uuid.uuid4().hex[:8]}.jpg"
        image_path = os.path.join(IMAGES_DIR, image_file_name_for_db)

        def save_and_ocr_sync(path, data_bytes):
            with open(path, "wb") as f: f.write(data_bytes)
            img_pil = Image.open(BytesIO(data_bytes))
            return pytesseract.image_to_string(img_pil, lang="spa+eng").strip()

        text_ocr = await asyncio.to_thread(save_and_ocr_sync, image_path, image_bytes)

        if not text_ocr:
            await send_whatsapp_message(telefono, f"🤔 {user_name_for_ocr_task}, no pude encontrar texto legible en la imagen. Para que pueda ayudarte mejor, asegúrate de que la imagen sea clara y el texto no sea muy pequeño o esté borroso. ¡Gracias!")
            return

        text_for_analysis = f"(El siguiente texto fue extraído de una imagen que me envió {user_name_for_ocr_task}. El OCR podría tener errores, por favor intenta entender el contexto original):\n---\n{text_ocr}\n---"

        image_context_for_handler = {
            "is_from_image_processing": True,
            "ocr_text_original": text_ocr,
            "image_db_id": image_file_name_for_db
        }
        await handle_registered_user_message(telefono, text_for_analysis, user_data, image_context=image_context_for_handler)

        print(f"Tarea de procesamiento de imagen para {telefono} ({user_name_for_ocr_task}) completada.")

    except pytesseract.TesseractNotFoundError:
        print("ERROR CRÍTICO: Tesseract OCR no está instalado o no en PATH.")
        await send_whatsapp_message(telefono, f"⚠️ ¡Uy, {user_name_for_ocr_task}! Parece que tengo un problema técnico con mi sistema para leer imágenes en este momento. Lamento no poder analizarla esta vez. Puedes intentarlo más tarde o enviarme el texto directamente si es posible.")
    except Exception as e:
        print(f"Error en process_incoming_image_task (tel: {telefono}, user: {user_name_for_ocr_task}): {e}")
        await send_whatsapp_message(telefono, f"⚠️ Lo siento mucho, {user_name_for_ocr_task}, ocurrió un error inesperado mientras procesaba tu imagen. Ya estoy enterado del problema. Por favor, intenta más tarde. 🙏")

async def handle_onboarding_process(telefono: str, text_received: str, user_data: sqlite3.Row):
    estado_actual = user_data["estado"]
    user_name_onboarding = user_data["nombre"] if user_data and user_data["nombre"] else "amigo/a" 

    if estado_actual == ESTADO_PENDIENTE_TERMINOS:  
        if "acepto" in text_received.lower():
            db_update_user(telefono, {"acepto_terminos": 1, "estado": ESTADO_PENDIENTE_NOMBRE})
            await send_whatsapp_message(telefono, "¡Excelente! 😊 Gracias por aceptar. Para que mis consejos sean aún mejores para ti, ¿podrías decirme tu nombre, por favor?")
        else: await send_whatsapp_message(telefono, "⚠️ Para que podamos continuar, necesito que aceptes los términos. Solo escribe *ACEPTO* si estás de acuerdo. ¡Gracias! 👍")
    
    elif estado_actual == ESTADO_PENDIENTE_NOMBRE: 
        ia_result_nombre = await analyze_with_deepseek(text_received, "nombre")
        if ia_result_nombre and ia_result_nombre.startswith("NOMBRE_VALIDO:"):
            nombre_extraido = ia_result_nombre.split(":", 1)[1].strip().title()
            db_update_user(telefono, {"nombre": nombre_extraido, "estado": ESTADO_PENDIENTE_EDAD})
            await send_whatsapp_message(telefono, f"¡Un placer conocerte, {nombre_extraido}! 👋 Ahora, si no es molestia, ¿me dirías cuántos años tienes? (Solo el número, por ejemplo: 35). Esto me ayuda a darte consejos más adecuados.")
        elif ia_result_nombre == "NOMBRE_INVALIDO":
            await send_whatsapp_message(telefono, "🤔 Mmm, eso no me parece un nombre de persona. ¿Podrías intentarlo de nuevo, por favor? Solo necesito tu primer nombre o cómo te gustaría que te llame. ¡Gracias!")
        else: # NOMBRE_CONFUSO o error de IA
            await send_whatsapp_message(telefono, "🤔 No estoy seguro de haber entendido tu nombre. ¿Podrías escribirlo de nuevo, un poquito más claro, por favor? ¡Gracias!")

    elif estado_actual == ESTADO_PENDIENTE_EDAD: 
        user_name_for_age_prompt = user_data["nombre"] if user_data and user_data["nombre"] else "gracias"
        ia_result_edad = await analyze_with_deepseek(text_received, "edad")
        if ia_result_edad and ia_result_edad.startswith("EDAD_VALIDA:"):
            try:
                edad_num = int(ia_result_edad.split(":", 1)[1])
                if 5 <= edad_num <= 120:
                    db_update_user(telefono, {"edad": edad_num, "estado": ESTADO_PENDIENTE_CONOCIMIENTO})
                    await send_whatsapp_message(telefono, f"¡Perfecto, {user_name_for_age_prompt}! 👍 Ya casi terminamos. Cuéntame, ¿qué tanto sabes sobre ciberseguridad y estafas en línea? Puedes responder: *Sí* (si sabes bastante), *Poco*, o *No* (si no sabes mucho). ¡Tu honestidad me ayuda a ayudarte mejor! 😊")
                else: 
                    await send_whatsapp_message(telefono, f"⚠️ Entendí el número {edad_num}, pero parece una edad un poco inusual, {user_name_for_age_prompt}. ¿Podrías confirmarla o escribirla de nuevo solo con números (por ejemplo: 28, 65)? ¡Gracias!")
            except ValueError: 
                 await send_whatsapp_message(telefono, f"⚠️ ¡Uy! Hubo un pequeño error al procesar la edad, {user_name_for_age_prompt}. ¿Podrías escribirla solo con números, como '60' o '35'? ¡Mil gracias!")
        elif ia_result_edad == "EDAD_INVALIDA":
            await send_whatsapp_message(telefono, f"🤔 {user_name_for_age_prompt}, eso no me parece una edad. ¿Podrías decirme cuántos años tienes usando números, por ejemplo '55'? ¡Gracias!")
        else: # EDAD_NO_CLARA o error de IA
            await send_whatsapp_message(telefono, f"🤔 No estoy seguro de haber entendido tu edad, {user_name_for_age_prompt}. Para que pueda ayudarte mejor, ¿podrías escribirla solo con números, por ejemplo '70'? ¡Gracias por tu paciencia!")

    elif estado_actual == ESTADO_PENDIENTE_CONOCIMIENTO: 
        user_name_final_step = user_data["nombre"] if user_data and user_data["nombre"] else "listo/a"
        ia_result_conocimiento = await analyze_with_deepseek(text_received, "conocimiento")
        
        if ia_result_conocimiento in ["Sí", "No", "Poco"]:
            db_update_user(telefono, {"conocimiento": ia_result_conocimiento, "estado": ESTADO_REGISTRADO}) 
            await send_whatsapp_message(telefono, f"¡Genial, {user_name_final_step}! ✅ ¡Hemos completado tu registro! Muchas gracias por tu tiempo y confianza. 🙏\n\n🛡️ A partir de ahora, estoy a tu disposición. Puedes enviarme cualquier mensaje de texto o imagen que te parezca sospechosa, y la analizaré contigo. También puedes hacerme preguntas sobre seguridad digital y cómo protegerte de fraudes en línea.\n\n¡Estoy aquí para ayudarte a navegar el mundo digital de forma más segura! 😊")
        else: # CONOCIMIENTO_AMBIGUO o error de IA
            await send_whatsapp_message(telefono, f"⚠️ Ups, {user_name_final_step}. No entendí bien tu respuesta sobre tu conocimiento. Para que pueda ayudarte mejor, ¿podrías decirme si sabes *Sí*, *Poco*, o *No* sobre ciberseguridad? ¡Una de esas tres opciones me ayuda mucho! 👍")

async def handle_post_phishing_response(telefono: str, text_received: str, user_data: sqlite3.Row):
    """Maneja la respuesta del usuario (SÍ/NO/AYUDA) después de un análisis de phishing."""
    user_profile_dict = dict(user_data)
    nombre_usuario = user_data["nombre"] if user_data and user_data["nombre"] else "tú"
    cleaned_text_upper = re.sub(r'\s+', ' ', text_received).strip().upper()

    if cleaned_text_upper == "SÍ" or cleaned_text_upper == "SI":
        await send_whatsapp_message(telefono, f"🆘 Entendido, {nombre_usuario}. No te preocupes, vamos a ver qué pasos puedes seguir. Dame un momento para prepararte la información... 🛡️")
        respuesta_ayuda = await analyze_with_deepseek( "El usuario indicó que SÍ interactuó con la estafa.", "ayuda_post_estafa", user_profile_dict)
        if respuesta_ayuda:
            await send_whatsapp_message(telefono, respuesta_ayuda)
        else:
            await send_whatsapp_message(telefono, f"Lo lamento, {nombre_usuario}, tuve dificultades para generar los pasos de ayuda en este momento. Si es urgente, te recomiendo contactar directamente a las autoridades o a un experto en seguridad. 🙏")
        db_update_user(telefono, {"estado": ESTADO_REGISTRADO}) 

    elif cleaned_text_upper == "NO":
        await send_whatsapp_message(telefono, f"¡Excelente noticia, {nombre_usuario}! 👍 Me alegra mucho que no hayas interactuado con ese mensaje sospechoso. ¡Eso demuestra que estás muy alerta! Sigue así, desconfiando y verificando siempre. Si tienes algo más que quieras analizar o alguna otra pregunta, no dudes en decírmelo. 😊")
        db_update_user(telefono, {"estado": ESTADO_REGISTRADO}) 

    elif cleaned_text_upper == "AYUDA":
        await send_whatsapp_message(telefono, f"🆘 De acuerdo, {nombre_usuario}. Te prepararé los pasos de ayuda específicos. Un momento, por favor... 🛡️")
        respuesta_ayuda = await analyze_with_deepseek("El usuario escribió AYUDA tras un análisis de estafa.", "ayuda_post_estafa", user_profile_dict)
        if respuesta_ayuda:
            await send_whatsapp_message(telefono, respuesta_ayuda)
        else:
            await send_whatsapp_message(telefono, f"Lo lamento, {nombre_usuario}, tuve dificultades para generar los pasos de ayuda en este momento. Si es urgente, te recomiendo contactar directamente a las autoridades o a un experto en seguridad. 🙏")
        db_update_user(telefono, {"estado": ESTADO_REGISTRADO}) 
    else:
        await send_whatsapp_message(telefono, f"🤔 {nombre_usuario}, no estoy seguro de haber entendido tu respuesta. A mi pregunta anterior sobre si interactuaste con el mensaje, por favor responde con *SÍ*, *NO*, o escribe *AYUDA* si necesitas los pasos a seguir. ¡Gracias!")
        # Mantenemos el estado ESTADO_ESPERANDO_RESPUESTA_PHISHING

async def handle_registered_user_message(telefono: str, text_received: str, user_data: sqlite3.Row, image_context: dict = None):
    cleaned_text = re.sub(r'\s+', ' ', text_received).strip()
    if not cleaned_text: 
        user_name_empty_msg = user_data["nombre"] if user_data and user_data["nombre"] else "Hola"
        await send_whatsapp_message(telefono, f"🤔 {user_name_empty_msg}, parece que me enviaste un mensaje vacío. ¿Necesitas ayuda con algo?")
        return

    user_profile_dict = dict(user_data) 
    intencion = await analyze_with_deepseek(cleaned_text, "intencion", user_profile_dict)
    nombre_usuario = user_data["nombre"] if user_data and user_data["nombre"] else "tú"

    if intencion == "analizar":
        await send_whatsapp_message(telefono, f"🔍 ¡Entendido, {nombre_usuario}! Estoy revisando el mensaje que me enviaste. Te aviso en un momento con mi análisis... 👍")
        analisis_phishing_completo = await analyze_with_deepseek(cleaned_text, "phishing", user_profile_dict)

        if analisis_phishing_completo:
            partes = analisis_phishing_completo.split("---DETALLES_SIGUEN---", 1)
            resumen_breve = partes[0].strip()
            detalles_completos = partes[1].strip() if len(partes) > 1 else ""

            await send_whatsapp_message(telefono, resumen_breve)

            db_updates = {
                "estado": ESTADO_ESPERANDO_MAS_DETALLES,
                "last_analysis_details": detalles_completos
            }
            if image_context and image_context.get("is_from_image_processing"):
                db_updates["last_image_ocr_text"] = image_context.get("ocr_text_original")
                db_updates["last_image_analysis_raw"] = analisis_phishing_completo
                db_updates["last_image_id_processed"] = image_context.get("image_db_id")
                db_updates["last_image_timestamp"] = datetime.datetime.now().isoformat()

            print(f"DEBUG: Intentando actualizar DB para {telefono} con los siguientes datos: {db_updates}")  # NEW LOG
            try:
                db_update_user(telefono, db_updates)
                print(f"DEBUG: Actualización de DB para {telefono} parece exitosa.")  # NEW LOG
            except Exception as e_db:
                print(f"ERROR CRÍTICO: Falló db_update_user para {telefono}. Datos: {db_updates}. Error: {e_db}")  # NEW LOG
                raise  # Re-raise to propagate error
        else:
            await send_whatsapp_message(telefono, f"Lo siento mucho, {nombre_usuario}, tuve un problema al intentar analizar tu mensaje. ¿Podrías intentarlo de nuevo un poco más tarde, por favor? 🙏")
    elif intencion == "saludo":
        await send_whatsapp_message(telefono, f"¡Hola de nuevo, {nombre_usuario}! 👋 ¿En qué te puedo ayudar hoy? 😊")
    elif intencion == "consulta_imagen_anterior":
        ocr_guardado = user_data.get("last_image_ocr_text")
        analisis_raw_guardado = user_data.get("last_image_analysis_raw")
        timestamp_guardado = user_data.get("last_image_timestamp")

        if ocr_guardado and analisis_raw_guardado:
            await send_whatsapp_message(telefono, f"La última imagen que analizamos ({timestamp_guardado}) contenía aproximadamente el siguiente texto:\n\n\"{ocr_guardado}\"\n\nSi quieres recordar mi análisis sobre ella, dime.")
        else:
            await send_whatsapp_message(telefono, f"No encuentro un análisis de imagen reciente en tu historial, {nombre_usuario}. ¿Quieres que analice una nueva?")
    else:
        await send_whatsapp_message(telefono, f"Vaya, {nombre_usuario}, no estoy completamente seguro de cómo ayudarte con eso. 🧐 ¿Podrías intentar expresarlo de otra manera o enviarme un mensaje sospechoso para que lo analice? Estoy aquí para los temas de ciberseguridad. 😊")

@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook_subscription(request: Request):
    if request.query_params.get("hub.mode") == "subscribe" and \
       request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        print("Verificación de Webhook exitosa.")
        return PlainTextResponse(request.query_params.get("hub.challenge", ""), status_code=200)
    print(f"Fallo en verificación de Webhook. Token: {request.query_params.get('hub.verify_token')}")
    raise HTTPException(status_code=403, detail="Verification token mismatch.")

@app.post("/webhook")
async def whatsapp_webhook_handler(request: Request):
    data = await request.json()
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        message_object = value.get("messages", [{}])[0]
        
        if not message_object: return JSONResponse(content={}, status_code=200)

        telefono_remitente = message_object.get("from")
        message_type = message_object.get("type")
        whatsapp_message_id = message_object.get("id")
        print(f"DEBUG: Received message with ID: {whatsapp_message_id} from {telefono_remitente}")

        if whatsapp_message_id in processed_message_ids:
            print(f"DEBUG: Duplicate webhook ignored for message_id: {whatsapp_message_id}")
            return JSONResponse(content={}, status_code=200)

        print(f"DEBUG: New message ID: {whatsapp_message_id}. Adding to processed_message_ids.")
        processed_message_ids.append(whatsapp_message_id) 
        
    except (KeyError, IndexError, TypeError) as e: 
        print(f"Error al parsear estructura básica del webhook: {e} - Data: {data}")
        return JSONResponse(content={}, status_code=200) 

    async with user_locks[telefono_remitente]:
        current_user = db_get_user(telefono_remitente)

        if not current_user:
            db_create_user(telefono_remitente)
            current_user = db_get_user(telefono_remitente) 
            if not current_user: 
                 print(f"Error CRÍTICO: No se pudo crear/leer usuario {telefono_remitente}.")
                 return JSONResponse(content={"status": "error interno"}, status_code=500) 

            await send_whatsapp_message(telefono_remitente,
                "👋 ¡Hola! Soy *SecurityBot-WA*, tu asistente virtual personal para ayudarte a navegar seguro en el mundo digital aquí en Colombia. 😊\n\n"
                "Para darte los mejores consejos y cumplir con la Ley 1581 de 2012 (protección de datos), necesito tu permiso para guardar algunos datos como tu número, y si me los das más adelante, tu nombre, edad y qué tanto sabes de ciberseguridad.\n\n"
                "🔒 Tu información es confidencial y solo la usaré para ayudarte mejor. ¡No la compartiré con nadie más!\n\n"
                "Puedes ver más sobre cómo manejo tus datos en nuestros términos y política de privacidad: [Enlace a tus Términos y Condiciones Actualizado]\n\n" 
                "👉 Si estás de acuerdo y quieres que te ayude, por favor responde con la palabra: *ACEPTO*"
            )
            return JSONResponse(content={}, status_code=200)

        user_state = current_user["estado"]
        user_name_for_handler = current_user["nombre"] if current_user and current_user["nombre"] else "tú"

        if user_state == ESTADO_ESPERANDO_RESPUESTA_PHISHING:
            if message_type == "text":
                text_recibido = message_object.get("text", {}).get("body", "").strip()
                if text_recibido:
                    await handle_post_phishing_response(telefono_remitente, text_recibido, current_user)
                else: 
                    await send_whatsapp_message(telefono_remitente, f"Por favor, {user_name_for_handler}, responde SÍ, NO o AYUDA a mi pregunta anterior. ¡Gracias! 😊")
            else: 
                await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, estaba esperando una respuesta de SÍ, NO o AYUDA. Si quieres analizar otra cosa, envíala después de responder, por favor. 👍")
        
        elif user_state < ESTADO_REGISTRADO: # Onboarding (0, 1, 2, 3)
            if message_type == "text":
                text_recibido = message_object.get("text", {}).get("body", "").strip()
                if text_recibido:
                    await handle_onboarding_process(telefono_remitente, text_recibido, current_user)
                else: await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, parece que no escribiste nada. Por favor, envía una respuesta para que podamos continuar. 😊")
            else: await send_whatsapp_message(telefono_remitente, f"¡Hola, {user_name_for_handler}! 😊 Para que podamos configurar tu perfil, necesito que me respondas con mensajes de texto a las preguntas anteriores. ¡Gracias!")
        
        elif user_state == ESTADO_REGISTRADO: 
            if message_type == "text":
                text_recibido = message_object.get("text", {}).get("body", "").strip()
                if text_recibido:
                    await handle_registered_user_message(telefono_remitente, text_recibido, current_user)
                else: await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, ¿necesitas ayuda con algo? Puedes enviarme un mensaje que te parezca sospechoso o hacerme una pregunta sobre seguridad. ¡Estoy aquí para ti! 👍")
            elif message_type == "image":
                image_id_wa = message_object.get("image", {}).get("id") 
                if image_id_wa:
                    await send_whatsapp_message(telefono_remitente, f"🖼️ ¡Recibí tu imagen, {user_name_for_handler}! La voy a revisar con cuidado y te envío mi análisis en un momento. 🧐")
                    asyncio.create_task(process_incoming_image_task(telefono_remitente, current_user, image_id_wa))
                else: await send_whatsapp_message(telefono_remitente, f"⚠️ Vaya, {user_name_for_handler}, parece que hubo un problema con la imagen que enviaste. ¿Podrías intentar mandarla de nuevo, por favor?")
            elif message_type == "audio": await send_whatsapp_message(telefono_remitente, f"¡Hola, {user_name_for_handler}! Recibí tu mensaje de audio. 🎤 Aún estoy aprendiendo a procesarlos, ¡pero espero poder ayudarte con ellos muy pronto! 😊")
            else: await send_whatsapp_message(telefono_remitente, f"Recibí un tipo de mensaje ({message_type}) que aún no sé cómo procesar del todo, {user_name_for_handler}. Por ahora, mi especialidad son los mensajes de texto e imágenes. 📄🖼️")
        
        elif user_state == ESTADO_ESPERANDO_MAS_DETALLES:
            if message_type == "text":
                text_recibido_mas_detalles = message_object.get("text", {}).get("body", "").strip().upper()
                if text_recibido_mas_detalles == "SÍ_DETALLES" or text_recibido_mas_detalles == "SI_DETALLES":
                    detalles_a_enviar = current_user["last_analysis_details"]
                    if detalles_a_enviar:
                        await send_whatsapp_message(telefono_remitente, detalles_a_enviar)

                        new_state = ESTADO_REGISTRADO
                        analisis_lower = detalles_a_enviar.lower()
                        cond_pregunta_hecha = "¿llegaste a hacer clic" in analisis_lower
                        cond_opciones_claras = ("sí o no" in analisis_lower or "si o no" in analisis_lower)
                        cond_opcion_ayuda = "escribe ayuda" in analisis_lower

                        if cond_pregunta_hecha and cond_opciones_claras and cond_opcion_ayuda:
                            new_state = ESTADO_ESPERANDO_RESPUESTA_PHISHING
                            print(f"INFO: Usuario {telefono_remitente} movido a estado ESPERANDO_RESPUESTA_PHISHING después de ver detalles.")

                        db_update_user(telefono_remitente, {"estado": new_state, "last_analysis_details": None})
                    else:
                        await send_whatsapp_message(telefono_remitente, "Parece que no tengo los detalles guardados. Por favor, envía el mensaje original de nuevo para analizarlo.")
                        db_update_user(telefono_remitente, {"estado": ESTADO_REGISTRADO, "last_analysis_details": None})
                else:
                    await send_whatsapp_message(telefono_remitente, f"🤔 {user_name_for_handler}, para ver el análisis detallado del mensaje anterior, por favor responde *SÍ_DETALLES*. Si quieres analizar algo nuevo o tienes otra consulta, envíamela.")
            else:
                await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, estaba esperando que me dijeras *SÍ_DETALLES* para darte más información. Si quieres analizar una nueva imagen o texto, envíamelo después de que respondas, por favor. 👍")
        
        else: 
            print(f"Error: Usuario {telefono_remitente} en estado desconocido: {user_state}")
            await send_whatsapp_message(telefono_remitente, f"¡Hola {user_name_for_handler}! Parece que hubo un pequeño error con mi memoria. ¿Podrías intentar enviarme tu mensaje de nuevo? Gracias. 😊")
            db_update_user(telefono_remitente, {"estado": ESTADO_REGISTRADO}) 

    return JSONResponse(content={}, status_code=200) 

if __name__ == "__main__":
    import uvicorn
    print("Iniciando servidor FastAPI localmente con Uvicorn...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
