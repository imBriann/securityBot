import os
import sqlite3
import asyncio
import httpx
import uuid
import re
import datetime
import random # Para los consejos de seguridad
import unicodedata # Para normalizar texto
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from io import BytesIO
from PIL import Image
import pytesseract
from dotenv import load_dotenv

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
ESTADO_ESPERANDO_RESPUESTA_PHISHING = 5
ESTADO_ESPERANDO_MAS_DETALLES = 6

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

# --- Funciones Auxiliares ---
def normalize_text(text: str) -> str:
    """Convierte a minúsculas, quita espacios extra y acentos comunes."""
    if not text:
        return ""
    text = text.lower().strip()
    # Quitar acentos comunes de vocales
    nfkd_form = unicodedata.normalize('NFKD', text)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def extract_first_url(text: str) -> str | None:
    """Extrae la primera URL encontrada en un texto."""
    if not text:
        return None
    # Expresión regular mejorada para URLs
    url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    match = re.search(url_pattern, text)
    return match.group(0) if match else None

SECURITY_TIPS = [
    "🛡️ Usa contraseñas únicas y fuertes para cada una de tus cuentas importantes. ¡Un gestor de contraseñas puede ayudarte mucho!",
    "🔒 Activa la verificación en dos pasos (2FA) siempre que esté disponible, especialmente en tu correo, redes sociales y bancos.",
    "❓ Desconfía de mensajes inesperados que te pidan información personal o te urjan a hacer clic en enlaces, ¡incluso si parecen de contactos conocidos!",
    "🔗 Antes de hacer clic en un enlace, especialmente en correos o mensajes, verifica que la dirección web (URL) sea legítima y no una imitación.",
    "🔄 Mantén tu sistema operativo, navegador y antivirus siempre actualizados para protegerte de las últimas amenazas.",
    "🚫 No descargues archivos de fuentes desconocidas o correos sospechosos, podrían contener malware.",
    "👀 Revisa periódicamente los permisos de las aplicaciones en tu teléfono y redes sociales. ¡Quita los que no necesites!",
    "💸 Sé muy cuidadoso con ofertas que parecen demasiado buenas para ser verdad, ¡usualmente lo son y pueden ser una estafa!",
    "📞 Si recibes una llamada o mensaje sospechoso de tu banco o una entidad, cuelga y contáctalos directamente a través de sus canales oficiales.",
    "📶 Evita conectarte a redes Wi-Fi públicas no seguras para realizar transacciones bancarias o ingresar información sensible."
]

def get_security_tip() -> str:
    """Devuelve un consejo de seguridad al azar."""
    return random.choice(SECURITY_TIPS)

# --- Funciones de Base de Datos ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

IMAGES_DIR = "imagenes_recibidas"
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR)

def setup_database(): # MODIFICADO
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
        last_image_timestamp DATETIME,
        last_analyzed_url TEXT 
    );
    """)
    # La tabla imagenes_procesadas puede mantenerse si se desea un log separado de solo imágenes
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
        print(f"DEBUG: db_update_user llamado para {telefono} sin datos. Retornando.")
        return

    fields = ", ".join([f"{key} = ?" for key in data])
    values = list(data.values())
    values.append(telefono)

    conn_db = None
    query = f"UPDATE usuarios SET {fields} WHERE telefono = ?"
    try:
        conn_db = get_db_connection()
        cursor_db = conn_db.cursor()
        print(f"DEBUG: Ejecutando SQL: {query} con valores (excepto el último que es el teléfono): {tuple(values[:-1])} para tel: {telefono}")
        cursor_db.execute(query, tuple(values))
        conn_db.commit()
        print(f"DEBUG: Commit exitoso para {telefono} en db_update_user.")
    except sqlite3.Error as e_sqlite:
        print(f"ERROR SQLITE en db_update_user para {telefono}: {e_sqlite}. Query: {query}, Values (sin token): {[(v[:20] + '...' if isinstance(v, str) and len(v) > 50 else v) for v in tuple(values)]}")
        if conn_db:
            conn_db.rollback()
        raise
    except Exception as e_general:
        print(f"ERROR GENERAL en db_update_user para {telefono}: {e_general}. Query: {query}, Values (sin token): {[(v[:20] + '...' if isinstance(v, str) and len(v) > 50 else v) for v in tuple(values)]}")
        if conn_db:
            conn_db.rollback()
        raise
    finally:
        if conn_db:
            conn_db.close()
            print(f"DEBUG: Conexión DB cerrada para {telefono} en db_update_user.")

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
    last_url_context = user_profile.get('last_analyzed_url', 'Ninguna') # Para el prompt de cyber_pregunta

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
        "intencion": { # MODIFICADO
            "system": (
                "Eres un asistente inteligente para WhatsApp. Tu tarea es analizar el siguiente mensaje de un usuario y determinar su intención principal. "
                "El usuario ya está registrado.\n"
                "Si el mensaje contiene un saludo (como 'gracias', 'hola') Y TAMBIÉN una pregunta o comando claro, prioriza la pregunta o comando como la intención principal.\n"
                "Responde SOLO con una de estas opciones (una sola palabra, en minúsculas y sin explicaciones adicionales):\n"
                "- saludo: si el mensaje ES PRINCIPALMENTE un saludo o una interacción social simple (ej: solo 'hola', solo 'gracias', 'ok', 'de nada').\n"
                "- analizar: si el usuario quiere que analices un mensaje de texto, el contenido de una imagen, o cualquier cosa que le parezca sospechosa de ser una estafa, phishing, fraude, o que contenga información engañosa.\n"
                "- pregunta_seguridad: si el usuario está haciendo una pregunta específica sobre ciberseguridad, cómo protegerse, qué es un tipo de estafa, etc. (que no sea simplemente reenviar un mensaje para analizar y no sea una pregunta sobre cómo usar el bot).\n"
                "- meta_pregunta: si el usuario está haciendo una pregunta sobre el bot mismo, sus capacidades, o cómo interactuar con él.\n"
                "- solicitar_tip_seguridad: si el usuario pide un consejo, tip o recomendación general de seguridad.\n"
                "- comando_reset: si el usuario quiere cancelar la operación actual y volver al inicio.\n"
                "- irrelevante: si el mensaje no tiene relación con los temas anteriores.\n\n"
                "Prioriza 'analizar' si el texto del mensaje parece ser el contenido de un mensaje sospechoso. "
                "Si hay un saludo y una pregunta de seguridad, la intención es 'pregunta_seguridad'."
            ),
            "user": message_text
        },
        "phishing": {
            "system": (
                f"Eres SecurityBot-WA, un asistente de seguridad digital en Colombia, muy AMABLE, EMPÁTICO y CLARO. Te diriges al usuario {user_name_for_prompt}.\n"
                f"Tu misión es revisar el siguiente mensaje y determinar si parece una estafa digital (phishing, smishing, etc.). Luego, crea una respuesta adaptada al perfil del usuario.\n\n"
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
                "PARTE 1 (Resumen Breve): Antes del separador '---DETALLES_SIGUEN---', proporciona un resumen MUY BREVE y directo (1-5 frases) sobre el mensaje analizado. Indica el riesgo principal (ej: '*Resumen Breve*:\\n{user_name_for_prompt}, este mensaje parece una estafa de tipo suplantación de identidad.' o '*Resumen Breve*:\\n{user_name_for_prompt}, en principio, este mensaje no parece ser una estafa.'). NO DES NINGUNA EXPLICACIÓN DETALLADA AQUÍ. El bot preguntará al usuario si desea más detalles después de este resumen.\n"
                "PARTE 2 (Análisis Completo): Después del separador '---DETALLES_SIGUEN---', incluye el análisis completo y detallado, manteniendo la siguiente estructura OBLIGATORIA:\n"
                "🔍 *Análisis del mensaje recibido*\n"
                "✅ *Resultado*: (Sí, parece una estafa / No, no parece una estafa / No estoy seguro, pero te doy recomendaciones)\n"
                "⚠️ *Tipo de estafa*: (Phishing, Smishing, Vishing, Fraude de soporte técnico, Suplantación de identidad, Malware, Sorteo falso, etc. o 'No aplica si no es estafa')\n"
                "📌 *Mi opinión detallada*: (Explica POR QUÉ llegaste a esa conclusión, adaptando la explicación al perfil de {user_name_for_prompt}. Señala las pistas o elementos sospechosos, o por qué no parece peligroso).\n"
                "🧠 *¿Cómo suelen funcionar estos engaños?* (Si es una estafa, explica brevemente el mecanismo de forma sencilla y adaptada al perfil. Si no es estafa, puedes omitir esta parte o dar un consejo general breve).\n"
                "🛡️ *Mis recomendaciones para ti, {user_name_for_prompt}*: (Consejos CLAROS, ÚTILES y FÁCILES de seguir. Si es estafa, qué hacer ahora. Si no lo es, cómo mantenerse alerta en general).\n\n"
                "IMPORTANTE (para la PARTE 2):\n"
                "- Si el análisis concluye que ES UNA ESTAFA (o altamente sospechoso), DEBES terminar tu respuesta (la PARTE 2) preguntando de forma amable: '{user_name_for_prompt}, ¿llegaste a hacer clic en algún enlace de ese mensaje, descargaste algo o compartiste información personal? Puedes responderme SÍ o NO. Si necesitas ayuda más específica sobre qué hacer si interactuaste, escribe AYUDA. ¡Estoy aquí para apoyarte! 😊'\n"
                "- Si NO ES UNA ESTAFA, finaliza la PARTE 2 con un mensaje positivo y de prevención general, por ejemplo: '¡Sigue así de alerta, {user_name_for_prompt}! Recuerda siempre desconfiar y verificar. 👍'\n"
                "- No uses saludos genéricos como 'Hola'. Ya te estás dirigiendo a {user_name_for_prompt}."
            ),
            "user": f"Por favor, {user_name_for_prompt} me envió este mensaje para analizarlo: \"{message_text}\""
        },
        "decision_ver_detalles": {
            "system": (
                "Eres un clasificador de intenciones para un chatbot de WhatsApp. El bot acaba de dar un resumen de un análisis de seguridad (phishing/estafa) y preguntó al usuario si quiere ver los detalles completos.\n"
                "El usuario ha respondido. Tu tarea es determinar si la respuesta del usuario significa que SÍ quiere ver los detalles, o si está diciendo OTRA COSA (una nueva pregunta, un comentario no relacionado, etc.).\n"
                "Considera que el usuario podría ser una persona mayor, así que sé flexible con respuestas afirmativas.\n\n"
                "Responde SOLO con una de estas dos opciones:\n"
                "- QUIERE_DETALLES: Si el usuario expresa afirmativamente que quiere ver los detalles. Ejemplos: \"Sí\", \"Claro\", \"Bueno\", \"Ok\", \"Mándamelos\", \"Más información por favor\", \"Sí quiero los detalles\", \"Dale\", \"Más\", \"Bueno sí\", \"A ver\", \"Quiero saber más\", \"Explícame\", \"Sí, por favor\", \"si\", \"mas informacion\".\n"
                "- OTRA_COSA: Si la respuesta del usuario NO es una clara afirmación para ver los detalles. Ejemplos: \"¿Y eso es peligroso?\", \"No gracias\", \"Qué es phishing?\", \"Entendido\", \"Ok gracias\", \"Y si ya abrí el enlace?\", o cualquier otra pregunta o comentario.\n\n"
                "No expliques nada más. Solo QUIERE_DETALLES u OTRA_COSA."
            ),
            "user": message_text
        },
        "decision_post_phishing_interaction": {
            "system": (
                f"Eres un clasificador de intenciones para un chatbot de WhatsApp llamado SecurityBot-WA. El bot acaba de determinar que un mensaje era una estafa y le preguntó al usuario ({user_name_for_prompt}) si interactuó con ella (SÍ/NO) o si necesita AYUDA.\n"
                "El usuario ha respondido. Tu tarea es clasificar esta respuesta.\n\n"
                "Responde SOLO con una de estas opciones:\n"
                "- RESPUESTA_SI: Si el usuario indica afirmativamente que SÍ interactuó con la estafa (ej: \"Sí\", \"Sí hice clic\", \"Creo que sí\", \"si\", \"claro\").\n" 
                "- RESPUESTA_NO: Si el usuario indica que NO interactuó con la estafa (ej: \"No\", \"No, para nada\", \"No hice nada\", \"nop\").\n" 
                "- PIDE_AYUDA: Si el usuario explícitamente pide ayuda o usa la palabra \"AYUDA\" (o variaciones como \"ayudame\").\n" 
                "- ES_PREGUNTA: Si el usuario hace una pregunta en lugar de responder directamente SÍ/NO/AYUDA (ej: \"¿Qué es phishing?\", \"¿Cómo puedo evitar esto?\", \"¿Y si ya di mis datos?\").\n"
                "- ES_COMENTARIO: Si el usuario hace un comentario, agradece, o da una respuesta corta que no es SÍ/NO/AYUDA ni una pregunta clara (ej: \"Gracias\", \"Ok\", \"Entendido\", \"Qué peligroso\", \"Es una estafa\").\n"
                "- OTRA_COSA: Si la respuesta es muy ambigua, no relacionada, o no encaja en las categorías anteriores.\n\n"
                "No expliques nada más. Solo una de las opciones listadas."
            ),
            "user": message_text
        },
        "ayuda_post_estafa": {
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
        "cyber_pregunta": { # MODIFICADO para incluir contexto de URL
            "system": (
                f"Eres SecurityBot-WA, un experto en ciberseguridad y fraudes digitales en Colombia, muy AMABLE, EDUCATIVO y PACIENTE. Te diriges al usuario {user_name_for_prompt}.\n"
                f"PERFIL DEL USUARIO ACTUAL: Nombre: {user_name_for_prompt}, Edad: {user_profile.get('edad', 'Desconocida')}, "
                f"Nivel de conocimiento en ciberseguridad: {user_profile.get('conocimiento', 'Desconocido')}, "
                f"Última URL analizada (si aplica y la pregunta parece relacionada): {last_url_context}.\n\n" # Contexto de URL añadido
                "INSTRUCCIONES DE TONO Y LENGUAJE:\n"
                f"- Dirígete a {user_name_for_prompt} por su nombre de forma natural.\n"
                "- Adapta tu lenguaje a su perfil (edad y conocimiento), similar a las instrucciones del modo 'phishing'. Explica conceptos complejos de forma sencilla.\n"
                "- Usa un tono positivo y alentador. El objetivo es educar y empoderar.\n"
                "- Usa emojis con moderación para hacer la explicación más amena (ej. 💡, 🛡️, 🤔, 👍, 😊).\n\n"
                "**NOTA ESPECIAL SOBRE TEXTO DE IMÁGENES (OCR)**: La pregunta de {user_name_for_prompt} podría provenir de una imagen y haber sido transcrita por un sistema OCR. Esto significa que PUEDE CONTENER ERRORES. Intenta inferir la pregunta real del usuario a pesar de las imperfecciones antes de responder.\n\n"
                "ESTRUCTURA DE LA RESPUESTA:\n"
                f"1.  Empieza con un saludo amable y reconociendo su pregunta, ej: '¡Hola, {user_name_for_prompt}! Claro, con gusto te explico sobre [tema de la pregunta]. 😊'\n"
                "2.  Explica el concepto o responde la pregunta de forma clara, concisa y adaptada. Si la pregunta parece referirse a la 'Última URL analizada', considera ese contexto en tu respuesta.\n"
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

    image_file_name_for_db = f"{telefono}_{uuid.uuid4().hex[:8]}.jpg"
    try:
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
        
        print(f"Tarea de procesamiento de imagen para {telefono} ({user_name_for_ocr_task}) completada exitosamente.")

    except pytesseract.TesseractNotFoundError:
        print("ERROR CRÍTICO: Tesseract OCR no está instalado o no en PATH.")
        await send_whatsapp_message(telefono, f"⚠️ ¡Uy, {user_name_for_ocr_task}! Parece que tengo un problema técnico con mi sistema para leer imágenes en este momento. Lamento no poder analizarla esta vez. Puedes intentarlo más tarde o enviarme el texto directamente si es posible.")
    except Exception as e:
        print(f"ERROR en process_incoming_image_task (tel: {telefono}, user: {user_name_for_ocr_task}, img_id_wa: {image_id_whatsapp}): {e}")
        await send_whatsapp_message(telefono, f"⚠️ Lo siento mucho, {user_name_for_ocr_task}, ocurrió un error inesperado mientras procesaba tu imagen. Ya estoy enterado del problema. Por favor, intenta más tarde. 🙏")

async def handle_onboarding_process(telefono: str, text_received: str, user_data: sqlite3.Row):
    estado_actual = user_data["estado"]
    user_name_onboarding = user_data["nombre"] if user_data and user_data["nombre"] else "amigo/a"

    if estado_actual == ESTADO_PENDIENTE_TERMINOS:
        normalized_text = normalize_text(text_received) 
        
        is_explicit_acceptance = "acepto" in normalized_text or \
                                 (normalized_text == "si") or \
                                 ("si acepto" in normalized_text)
                                 
        is_explicit_rejection = "no acepto" in normalized_text or \
                                "no quiero" in normalized_text or \
                                "no estoy de acuerdo" in normalized_text or \
                                normalized_text == "no"

        if is_explicit_acceptance and not ("no" in normalized_text and "acepto" not in normalized_text): 
            db_update_user(telefono, {"acepto_terminos": 1, "estado": ESTADO_PENDIENTE_NOMBRE})
            await send_whatsapp_message(telefono, "¡Excelente! 😊 Gracias por aceptar. Para que mis consejos sean aún mejores para ti, ¿podrías decirme tu nombre, por favor?")
        elif is_explicit_rejection:
            await send_whatsapp_message(telefono, "Entendido. Si cambias de opinión y deseas aceptar los términos para usar mis servicios, solo escribe *ACEPTO*. ¡Estaré aquí para ayudarte! 👍")
        else: 
            await send_whatsapp_message(telefono, "⚠️ Para que podamos continuar, necesito que aceptes los términos. Solo escribe *ACEPTO* si estás de acuerdo. Si no deseas continuar, puedes responder *NO ACEPTO*. ¡Gracias! 👍")

    elif estado_actual == ESTADO_PENDIENTE_NOMBRE:
        ia_result_nombre = await analyze_with_deepseek(text_received, "nombre")
        if ia_result_nombre and ia_result_nombre.startswith("NOMBRE_VALIDO:"):
            nombre_extraido = ia_result_nombre.split(":", 1)[1].strip().title()
            db_update_user(telefono, {"nombre": nombre_extraido, "estado": ESTADO_PENDIENTE_EDAD})
            await send_whatsapp_message(telefono, f"¡Un placer conocerte, {nombre_extraido}! 👋 Ahora, si no es molestia, ¿me dirías cuántos años tienes? (Solo el número, por ejemplo: 35). Esto me ayuda a darte consejos más adecuados.")
        elif ia_result_nombre == "NOMBRE_INVALIDO":
            await send_whatsapp_message(telefono, "🤔 Mmm, eso no me parece un nombre de persona. ¿Podrías intentarlo de nuevo, por favor? Solo necesito tu primer nombre o cómo te gustaría que te llame. ¡Gracias!")
        else: 
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
        else: 
            await send_whatsapp_message(telefono, f"🤔 No estoy seguro de haber entendido tu edad, {user_name_for_age_prompt}. Para que pueda ayudarte mejor, ¿podrías escribirla solo con números, por ejemplo '70'? ¡Gracias por tu paciencia!")

    elif estado_actual == ESTADO_PENDIENTE_CONOCIMIENTO:
        user_name_final_step = user_data["nombre"] if user_data and user_data["nombre"] else "listo/a"
        ia_result_conocimiento = await analyze_with_deepseek(text_received, "conocimiento")

        if ia_result_conocimiento in ["Sí", "No", "Poco"]:
            db_update_user(telefono, {"conocimiento": ia_result_conocimiento, "estado": ESTADO_REGISTRADO})
            await send_whatsapp_message(telefono, f"¡Genial, {user_name_final_step}! ✅ ¡Hemos completado tu registro! Muchas gracias por tu tiempo y confianza. 🙏\n\n🛡️ A partir de ahora, estoy a tu disposición. Puedes enviarme cualquier mensaje de texto o imagen que te parezca sospechosa, y la analizaré contigo. También puedes hacerme preguntas sobre seguridad digital y cómo protegerte de fraudes en línea.\n\n¡Estoy aquí para ayudarte a navegar el mundo digital de forma más segura! 😊")
        else: 
            await send_whatsapp_message(telefono, f"⚠️ Ups, {user_name_final_step}. No entendí bien tu respuesta sobre tu conocimiento. Para que pueda ayudarte mejor, ¿podrías decirme si sabes *Sí*, *Poco*, o *No* sobre ciberseguridad? ¡Una de esas tres opciones me ayuda mucho! 👍")


async def handle_post_phishing_response(telefono: str, text_received: str, user_data: sqlite3.Row):
    user_profile_dict = dict(user_data)
    nombre_usuario = user_data["nombre"] if user_data and user_data["nombre"] else "tú"
    normalized_input = normalize_text(text_received) 
    
    decision_usuario = await analyze_with_deepseek(normalized_input, "decision_post_phishing_interaction", user_profile_dict)
    print(f"DEBUG: Decisión IA en handle_post_phishing_response ({telefono}): {decision_usuario} para texto normalizado: '{normalized_input}' (original: '{text_received}')")

    re_prompt_after_digression = f"Espero que eso haya aclarado tu duda, {nombre_usuario}. Recordando nuestra conversación anterior sobre el mensaje sospechoso, ¿llegaste a interactuar con él (SÍ/NO) o necesitas AYUDA específica?"
    re_prompt_after_comment = f"Entendido, {nombre_usuario}. Volviendo al tema importante: sobre el mensaje que analizamos, ¿llegaste a interactuar con él (SÍ/NO) o necesitas AYUDA específica?"
    re_prompt_generic = f"🤔 {nombre_usuario}, no estoy seguro de haber entendido tu respuesta. A mi pregunta anterior sobre si interactuaste con el mensaje, por favor responde con *SÍ*, *NO*, o escribe *AYUDA* si necesitas los pasos a seguir. ¡Gracias!"


    if decision_usuario == "RESPUESTA_SI":
        await send_whatsapp_message(telefono, f"🆘 Entendido, {nombre_usuario}. No te preocupes, vamos a ver qué pasos puedes seguir. Dame un momento para prepararte la información... 🛡️")
        respuesta_ayuda = await analyze_with_deepseek( "El usuario indicó que SÍ interactuó con la estafa.", "ayuda_post_estafa", user_profile_dict)
        if respuesta_ayuda:
            await send_whatsapp_message(telefono, respuesta_ayuda)
        else:
            await send_whatsapp_message(telefono, f"Lo lamento, {nombre_usuario}, tuve dificultades para generar los pasos de ayuda en este momento. Si es urgente, te recomiendo contactar directamente a las autoridades o a un experto en seguridad. 🙏")
        db_update_user(telefono, {"estado": ESTADO_REGISTRADO, "last_analyzed_url": None}) # Limpiar URL también

    elif decision_usuario == "RESPUESTA_NO":
        await send_whatsapp_message(telefono, f"¡Excelente noticia, {nombre_usuario}! 👍 Me alegra mucho que no hayas interactuado con ese mensaje sospechoso. ¡Eso demuestra que estás muy alerta! Sigue así, desconfiando y verificando siempre. Si tienes algo más que quieras analizar o alguna otra pregunta, no dudes en decírmelo. 😊")
        db_update_user(telefono, {"estado": ESTADO_REGISTRADO, "last_analyzed_url": None}) # Limpiar URL también

    elif decision_usuario == "PIDE_AYUDA":
        await send_whatsapp_message(telefono, f"🆘 De acuerdo, {nombre_usuario}. Te prepararé los pasos de ayuda específicos. Un momento, por favor... 🛡️")
        respuesta_ayuda = await analyze_with_deepseek("El usuario escribió AYUDA tras un análisis de estafa.", "ayuda_post_estafa", user_profile_dict)
        if respuesta_ayuda:
            await send_whatsapp_message(telefono, respuesta_ayuda)
        else:
            await send_whatsapp_message(telefono, f"Lo lamento, {nombre_usuario}, tuve dificultades para generar los pasos de ayuda en este momento. Si es urgente, te recomiendo contactar directamente a las autoridades o a un experto en seguridad. 🙏")
        db_update_user(telefono, {"estado": ESTADO_REGISTRADO, "last_analyzed_url": None}) # Limpiar URL también

    elif decision_usuario == "ES_PREGUNTA":
        print(f"DEBUG: Usuario {telefono} hizo una pregunta en estado ESPERANDO_RESPUESTA_PHISHING: '{text_received}'")
        await send_whatsapp_message(telefono, f"🤔 ¡Claro, {nombre_usuario}! Déjame responder tu pregunta sobre \"{text_received[:30]}...\". Un momento...")
        # Pasar el user_profile completo que incluye last_analyzed_url
        respuesta_a_pregunta = await analyze_with_deepseek(text_received, "cyber_pregunta", user_profile_dict)
        if respuesta_a_pregunta:
            await send_whatsapp_message(telefono, respuesta_a_pregunta)
            await send_whatsapp_message(telefono, re_prompt_after_digression)
        else:
            await send_whatsapp_message(telefono, f"Mis disculpas, {nombre_usuario}, no pude procesar tu pregunta en este momento. {re_prompt_after_digression}")
        # El estado sigue siendo ESTADO_ESPERANDO_RESPUESTA_PHISHING

    elif decision_usuario == "ES_COMENTARIO":
        print(f"DEBUG: Usuario {telefono} hizo un comentario en estado ESPERANDO_RESPUESTA_PHISHING: '{text_received}'")
        if "gracias" in normalized_input:
            await send_whatsapp_message(telefono, f"¡De nada, {nombre_usuario}! 😊 {re_prompt_after_comment}")
        elif "ok" in normalized_input or "entendido" in normalized_input:
            await send_whatsapp_message(telefono, f"Entendido, {nombre_usuario}. {re_prompt_after_comment}")
        else: 
            await send_whatsapp_message(telefono, f"Ok, {nombre_usuario}, he tomado nota de tu comentario. {re_prompt_after_comment}")
        # El estado sigue siendo ESTADO_ESPERANDO_RESPUESTA_PHISHING
        
    else: # OTRA_COSA o error de IA
        print(f"DEBUG: Respuesta no clasificada ({decision_usuario}) en ESPERANDO_RESPUESTA_PHISHING para {telefono}. Texto: '{text_received}'")
        await send_whatsapp_message(telefono, re_prompt_generic)
        # El estado sigue siendo ESTADO_ESPERANDO_RESPUESTA_PHISHING

async def handle_registered_user_message(telefono: str, text_received: str, user_data: sqlite3.Row, image_context: dict = None): # MODIFICADO
    cleaned_text = re.sub(r'\s+', ' ', text_received).strip() 
    nombre_usuario = user_data["nombre"] if user_data and user_data["nombre"] else "tú" 

    if not cleaned_text:
        await send_whatsapp_message(telefono, f"🤔 {nombre_usuario}, parece que me enviaste un mensaje vacío. ¿Necesitas ayuda con algo?")
        return

    user_profile_dict = dict(user_data)
    intencion = await analyze_with_deepseek(cleaned_text, "intencion", user_profile_dict) 
    print(f"DEBUG: Intención clasificada para {telefono} ({nombre_usuario}): {intencion} para texto: '{cleaned_text[:50]}...'")

    if intencion == "comando_reset":
        await send_whatsapp_message(telefono, f"De acuerdo, {nombre_usuario}. Hemos vuelto al menú principal. ¿En qué te puedo ayudar ahora? 😊")
        db_update_user(telefono, {
            "estado": ESTADO_REGISTRADO,
            "last_analysis_details": None,
            "last_image_ocr_text": None,
            "last_image_analysis_raw": None,
            "last_image_id_processed": None,
            "last_image_timestamp": None,
            "last_analyzed_url": None # Limpiar URL también
        })
        return

    if intencion == "saludo":
        greeting = f"¡Hola de nuevo, {nombre_usuario}! 👋"
        last_interaction_info = ""
        if "last_image_timestamp" in user_data:  # Fix: Access directly as dictionary
            last_interaction_info = " La última vez que interactuamos fue sobre un análisis reciente."
        elif "last_analyzed_url" in user_data:
             last_interaction_info = " Recientemente analizamos un enlace."
        
        greeting += last_interaction_info
        greeting += " ¿En qué te puedo ayudar hoy? 😊"
        await send_whatsapp_message(telefono, greeting)

    elif intencion == "analizar":
        await send_whatsapp_message(telefono, f"🔍 ¡Entendido, {nombre_usuario}! Estoy revisando el mensaje que me enviaste. Te aviso en un momento con mi análisis... 👍")
        
        # Extraer URL antes de enviar a la IA de phishing, para guardarla
        extracted_url = extract_first_url(cleaned_text)
        if not extracted_url and image_context and image_context.get("ocr_text_original"): # Si no hay URL en texto, pero es imagen, buscar en OCR
            extracted_url = extract_first_url(image_context.get("ocr_text_original"))

        analisis_phishing_completo = await analyze_with_deepseek(cleaned_text, "phishing", user_profile_dict)

        if analisis_phishing_completo:
            partes = analisis_phishing_completo.split("---DETALLES_SIGUEN---", 1)
            resumen_breve = partes[0].strip()
            detalles_completos = partes[1].strip() if len(partes) > 1 else ""

            await send_whatsapp_message(telefono, resumen_breve)
            await send_whatsapp_message(telefono, f"{nombre_usuario}, ¿quieres que te dé más detalles y mis recomendaciones sobre esto? 😊") 

            db_updates = {
                "estado": ESTADO_ESPERANDO_MAS_DETALLES,
                "last_analysis_details": detalles_completos,
                "last_analyzed_url": extracted_url # Guardar la URL extraída
            }
            if image_context and image_context.get("is_from_image_processing"):
                db_updates["last_image_ocr_text"] = image_context.get("ocr_text_original")
                db_updates["last_image_analysis_raw"] = analisis_phishing_completo
                db_updates["last_image_id_processed"] = image_context.get("image_db_id")
                db_updates["last_image_timestamp"] = datetime.datetime.now().isoformat()
            
            print(f"DEBUG: Intentando actualizar DB para {telefono} con los siguientes datos: {db_updates}")
            try:
                db_update_user(telefono, db_updates)
                print(f"DEBUG: Actualización de DB para {telefono} (estado a ESPERANDO_MAS_DETALLES) parece exitosa.")
            except Exception as e_db_update:
                print(f"ERROR CRÍTICO: Falló db_update_user tras enviar resumen para {telefono}. Datos: {db_updates}. Error: {e_db_update}")
                raise e_db_update
        else:
            await send_whatsapp_message(telefono, f"Lo siento mucho, {nombre_usuario}, tuve un problema al intentar analizar tu mensaje. ¿Podrías intentarlo de nuevo un poco más tarde, por favor? 🙏")
        # El mensaje de cierre se mueve al webhook handler después de enviar los detalles completos.

    elif intencion == "meta_pregunta":
        normalized_meta_pregunta = normalize_text(cleaned_text)
        if "imagen" in normalized_meta_pregunta and ("puedo" in normalized_meta_pregunta or "enviar" in normalized_meta_pregunta or "mandar" in normalized_meta_pregunta):
            await send_whatsapp_message(telefono, f"¡Claro que sí, {nombre_usuario}! Puedes enviarme imágenes que te parezcan sospechosas y las analizaré para ti. 🖼️👍")
        elif "que haces" in normalized_meta_pregunta or "para que sirves" in normalized_meta_pregunta or "como funcionas" in normalized_meta_pregunta:
            await send_whatsapp_message(telefono, f"Soy SecurityBot-WA, {nombre_usuario}. Estoy aquí para ayudarte a analizar mensajes de texto o imágenes que te parezcan sospechosas de ser estafas o phishing. También puedo responder tus preguntas sobre ciberseguridad y cómo protegerte en línea, o darte consejos de seguridad. 😊")
        elif "audio" in normalized_meta_pregunta and ("entiendes" in normalized_meta_pregunta or "procesas" in normalized_meta_pregunta):
            await send_whatsapp_message(telefono, f"¡Hola, {nombre_usuario}! Por el momento, mi especialidad son los mensajes de texto e imágenes. Aún estoy aprendiendo a procesar audios, ¡pero espero poder ayudarte con ellos muy pronto! 😊")
        else: 
            await send_whatsapp_message(telefono, f"Entendido, {nombre_usuario}. Si tienes un mensaje o imagen para analizar, ¡envíamelo! O si tienes una pregunta sobre ciberseguridad o quieres un consejo, también puedo ayudarte con eso. 😊")

    elif intencion == "pregunta_seguridad":
        await send_whatsapp_message(telefono, f"🤔 ¡Buena pregunta sobre seguridad, {nombre_usuario}! Déjame consultar mis datos para darte la mejor respuesta. Un momento, por favor... 💡")
        # Pasamos user_profile_dict que ya contiene last_analyzed_url si existe
        respuesta_pregunta = await analyze_with_deepseek(cleaned_text, "cyber_pregunta", user_profile_dict)
        if respuesta_pregunta: await send_whatsapp_message(telefono, respuesta_pregunta)
        else: await send_whatsapp_message(telefono, f"Mis disculpas, {nombre_usuario}. Parece que tuve un inconveniente al procesar tu pregunta de seguridad. ¿Podrías intentar reformularla o consultarme de nuevo en un momento? Gracias por tu paciencia. 😊")
        await send_whatsapp_message(telefono, f"Espero que esta información te sea útil, {nombre_usuario}. 👍")


    elif intencion == "solicitar_tip_seguridad": 
        tip = get_security_tip()
        await send_whatsapp_message(telefono, f"¡Claro, {nombre_usuario}! Aquí tienes un consejo de seguridad para ti:\n\n{tip}\n\nEspero te sea útil. 😊")

    elif intencion == "irrelevante" or not intencion : 
        print(f"Intención clasificada como '{intencion}' o no clasificada para '{cleaned_text[:50]}...' de {nombre_usuario}.")
        await send_whatsapp_message(telefono, f"Vaya, {nombre_usuario}, no estoy completamente seguro de cómo ayudarte con eso. 🤔\nRecuerda que puedo:\n1. Analizar un mensaje o imagen sospechosa 🔍\n2. Responder preguntas sobre ciberseguridad 🛡️\n3. Darte un consejo de seguridad rápido 💡\n\n¿Qué te gustaría hacer? Puedes enviar el mensaje/imagen a analizar, tu pregunta, o escribir 'consejo'.")
    
    else: 
        print(f"Intención NO MANEJADA o error de IA para '{cleaned_text[:50]}...' de {nombre_usuario}: {intencion}")
        await send_whatsapp_message(telefono, f"Vaya, {nombre_usuario}, no estoy completamente seguro de cómo ayudarte con eso. 🧐 ¿Podrías intentar expresarlo de otra manera o enviarme un mensaje sospechoso para que lo analice? Estoy aquí para los temas de ciberseguridad. 😊")


@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook_subscription(request: Request):
    if request.query_params.get("hub.mode") == "subscribe" and \
       request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        print("Verificación de Webhook exitosa.")
        return PlainTextResponse(request.query_params.get("hub.challenge", ""), status_code=200)
    print(f"Fallo en verificación de Webhook. Token: {request.query_params.get('hub.verify_token')}")
    raise HTTPException(status_code=403, detail="Verification token mismatch.")

@app.post("/webhook") # MODIFICADO para manejo de feedback y reset
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
        text_recibido_original = "" 
        if message_type == "text":
            text_recibido_original = message_object.get("text", {}).get("body", "").strip()

        print(f"DEBUG: Webhook IN: Tel: {telefono_remitente}, MsgID: {whatsapp_message_id}, Type: {message_type}, Text: '{text_recibido_original[:50]}...'")

        if not telefono_remitente or not whatsapp_message_id:
            print(f"Webhook ignorado: falta telefono_remitente o whatsapp_message_id. Tel: {telefono_remitente}, MsgID: {whatsapp_message_id}")
            return JSONResponse(content={}, status_code=200)

        if whatsapp_message_id in processed_message_ids:
            print(f"Webhook duplicado ignorado para message_id: {whatsapp_message_id}")
            return JSONResponse(content={}, status_code=200)
        
        normalized_text_for_cmd_check = normalize_text(text_recibido_original)
        reset_commands = ["empezar de nuevo", "reset", "cancelar", "olvidalo", "ya no", "detente"]
        is_reset_command = any(cmd in normalized_text_for_cmd_check for cmd in reset_commands) and \
                           (len(normalized_text_for_cmd_check) < 30 or normalized_text_for_cmd_check in reset_commands)

        if is_reset_command:
            async with user_locks[telefono_remitente]: # Asegurar acceso a DB
                current_user_for_reset = db_get_user(telefono_remitente)
                user_name_for_reset = current_user_for_reset["nombre"] if current_user_for_reset and current_user_for_reset["nombre"] else "tú"
                
                await send_whatsapp_message(telefono_remitente, f"De acuerdo, {user_name_for_reset}. Hemos cancelado la operación actual y volvemos al inicio. ¿En qué te puedo ayudar? 😊")
                db_update_user(telefono_remitente, {
                    "estado": ESTADO_REGISTRADO,
                    "last_analysis_details": None,
                    "last_image_ocr_text": None,
                    "last_image_analysis_raw": None,
                    "last_image_id_processed": None,
                    "last_image_timestamp": None,
                    "last_analyzed_url": None
                })
                processed_message_ids.append(whatsapp_message_id) 
            return JSONResponse(content={}, status_code=200)
        
        # Manejo de feedback simple (pulgares)
        if message_type == "text" and (text_recibido_original == "👍" or text_recibido_original == "👎"):
            async with user_locks[telefono_remitente]: # Asegurar acceso a DB
                current_user_for_feedback = db_get_user(telefono_remitente)
                if current_user_for_feedback and current_user_for_feedback["estado"] == ESTADO_REGISTRADO: # Solo si está en estado general
                    print(f"FEEDBACK recibido de {telefono_remitente}: {text_recibido_original}")
                    await send_whatsapp_message(telefono_remitente, "¡Gracias por tu feedback! 😊")
                    # Aquí podrías añadir lógica para guardar el feedback en la DB si lo deseas.
                    # Por ejemplo: db_log_feedback(telefono_remitente, text_recibido_original)
                    processed_message_ids.append(whatsapp_message_id)
                    return JSONResponse(content={}, status_code=200)
        
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
                "👋 ¡Hola! Soy SecurityBot-WA, tu asistente virtual para ayudarte a navegar seguro en el mundo digital en Colombia. 😊\n\n"
                "Para darte la mejor orientación y cumplir con la Ley 1581 de 2012 (protección de datos personales), necesito tu autorización para guardar algunos datos como tu número de teléfono, y más adelante, tu nombre, edad y nivel de conocimiento en ciberseguridad.\n\n"
                "🔒 Tu información será confidencial y se usará exclusivamente para mejorar tu experiencia. ¡Nunca la compartiré con terceros!\n\n"
                "📄 Puedes conocer más detalles en nuestros Términos y Política de Privacidad: https://drive.google.com/file/d/1x7fp9FO3vRGaRcpEeJTbVa050B5aordr/view?usp=sharing\n\n"
                "👉 Si estás de acuerdo, por favor responde con: ACEPTO"
            )
            return JSONResponse(content={}, status_code=200)

        user_state = current_user["estado"]
        user_name_for_handler = current_user["nombre"] if current_user and current_user["nombre"] else "tú"
        print(f"DEBUG: Handler para {telefono_remitente}, Estado: {user_state}")

        if user_state == ESTADO_ESPERANDO_MAS_DETALLES:
            if message_type == "text":
                print(f"DEBUG: {telefono_remitente} en ESPERANDO_MAS_DETALLES, recibió: '{text_recibido_original}'")
                user_profile_dict = dict(current_user)
                decision_ia = await analyze_with_deepseek(normalize_text(text_recibido_original), "decision_ver_detalles", user_profile_dict)
                print(f"DEBUG: Decisión de IA para ver detalles ({telefono_remitente}): {decision_ia}")

                if decision_ia == "QUIERE_DETALLES":
                    detalles_a_enviar = current_user["last_analysis_details"]
                    if detalles_a_enviar:
                        await send_whatsapp_message(telefono_remitente, detalles_a_enviar)
                        # Pregunta de feedback
                        await send_whatsapp_message(telefono_remitente, f"{user_name_for_handler}, ¿te fue útil este análisis? Puedes responder con un 👍 o 👎, o simplemente seguir con otra consulta.")
                        
                        new_state_after_details = ESTADO_REGISTRADO
                        analisis_lower = detalles_a_enviar.lower()
                        cond_pregunta_hecha = "¿llegaste a hacer clic" in analisis_lower
                        cond_opciones_claras = ("sí o no" in analisis_lower or "si o no" in analisis_lower)
                        cond_opcion_ayuda = "escribe ayuda" in analisis_lower

                        if cond_pregunta_hecha and cond_opciones_claras and cond_opcion_ayuda:
                            new_state_after_details = ESTADO_ESPERANDO_RESPUESTA_PHISHING
                            print(f"INFO: Usuario {telefono_remitente} movido a estado ESPERANDO_RESPUESTA_PHISHING después de ver detalles.")
                        db_update_user(telefono_remitente, {"estado": new_state_after_details, "last_analysis_details": None}) 
                    else:
                        await send_whatsapp_message(telefono_remitente, "Parece que no tengo los detalles guardados. Por favor, envía el mensaje original de nuevo para analizarlo.")
                        db_update_user(telefono_remitente, {"estado": ESTADO_REGISTRADO, "last_analysis_details": None})
                elif decision_ia == "OTRA_COSA":
                    print(f"DEBUG: {telefono_remitente} dijo OTRA_COSA. Tratando como nueva consulta.")
                    db_update_user(telefono_remitente, {"estado": ESTADO_REGISTRADO, "last_analysis_details": None}) 
                    current_user_reloaded = db_get_user(telefono_remitente) 
                    if current_user_reloaded: 
                         await handle_registered_user_message(telefono_remitente, text_recibido_original, current_user_reloaded)
                    else: 
                        print(f"ERROR: No se pudo recargar el usuario {telefono_remitente} después de OTRA_COSA.")
                        await send_whatsapp_message(telefono_remitente, "Hubo un pequeño problema, ¿podrías enviar tu consulta de nuevo, por favor?")
                else: 
                    print(f"WARN: Respuesta no esperada de IA para decision_ver_detalles ({telefono_remitente}): {decision_ia}")
                    await send_whatsapp_message(telefono_remitente, f"🤔 {user_name_for_handler}, no estoy seguro de cómo proceder. Si querías ver los detalles, puedes intentarlo de nuevo diciendo 'sí, quiero verlos'. Si era otra consulta, por favor envíamela de nuevo.")
            else: 
                await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, esperaba un mensaje de texto para saber si querías más detalles. Si es así, por favor, escribe algo como 'sí, muéstrame'. Si era otra cosa, puedes enviármelo.")

        elif user_state == ESTADO_ESPERANDO_RESPUESTA_PHISHING:
            if message_type == "text":
                if text_recibido_original:
                    await handle_post_phishing_response(telefono_remitente, text_recibido_original, current_user)
                else: 
                    await send_whatsapp_message(telefono_remitente, f"Por favor, {user_name_for_handler}, responde SÍ, NO o AYUDA a mi pregunta anterior. ¡Gracias! 😊")
            else: 
                await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, estaba esperando una respuesta de SÍ, NO o AYUDA en texto. Si quieres analizar otra cosa, envíala después de responder, por favor. 👍")

        elif user_state < ESTADO_REGISTRADO: 
            if message_type == "text":
                if text_recibido_original:
                    await handle_onboarding_process(telefono_remitente, text_recibido_original, current_user)
                else: await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, parece que no escribiste nada. Por favor, envía una respuesta para que podamos continuar. 😊")
            else: await send_whatsapp_message(telefono_remitente, f"¡Hola, {user_name_for_handler}! 😊 Para que podamos configurar tu perfil, necesito que me respondas con mensajes de texto a las preguntas anteriores. ¡Gracias!")

        elif user_state == ESTADO_REGISTRADO:
            if message_type == "text":
                if text_recibido_original:
                    await handle_registered_user_message(telefono_remitente, text_recibido_original, current_user)
                else: await send_whatsapp_message(telefono_remitente, f"Hola {user_name_for_handler}, ¿necesitas ayuda con algo? Puedes enviarme un mensaje que te parezca sospechoso o hacerme una pregunta sobre seguridad. ¡Estoy aquí para ti! 👍")
            elif message_type == "image":
                image_id_wa = message_object.get("image", {}).get("id")
                if image_id_wa:
                    await send_whatsapp_message(telefono_remitente, f"🖼️ ¡Recibí tu imagen, {user_name_for_handler}! La voy a revisar con cuidado y te envío mi análisis en un momento. 🧐")
                    asyncio.create_task(process_incoming_image_task(telefono_remitente, current_user, image_id_wa))
                else: await send_whatsapp_message(telefono_remitente, f"⚠️ Vaya, {user_name_for_handler}, parece que hubo un problema con la imagen que enviaste. ¿Podrías intentar mandarla de nuevo, por favor?")
            elif message_type == "audio": await send_whatsapp_message(telefono_remitente, f"¡Hola, {user_name_for_handler}! Recibí tu mensaje de audio. 🎤 Aún estoy aprendiendo a procesarlos, ¡pero espero poder ayudarte con ellos muy pronto! 😊")
            else: await send_whatsapp_message(telefono_remitente, f"Recibí un tipo de mensaje ({message_type}) que aún no sé cómo procesar del todo, {user_name_for_handler}. Por ahora, mi especialidad son los mensajes de texto e imágenes. 📄🖼️")

        else:
            print(f"Error: Usuario {telefono_remitente} en estado desconocido: {user_state}")
            await send_whatsapp_message(telefono_remitente, f"¡Hola {user_name_for_handler}! Parece que hubo un pequeño error con mi memoria. ¿Podrías intentar enviarme tu mensaje de nuevo? Gracias. 😊")
            db_update_user(telefono_remitente, {"estado": ESTADO_REGISTRADO})

    return JSONResponse(content={}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    print("Iniciando servidor FastAPI localmente con Uvicorn...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
