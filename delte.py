import os

# Ruta de la base de datos (ajusta si está en otro directorio)
db_path = "usuarios_bot.db"

try:
    if os.path.exists(db_path):
        os.remove(db_path)
        print("✅ Base de datos eliminada correctamente.")
    else:
        print("⚠️ La base de datos no existe.")
except Exception as e:
    print(f"❌ Error al eliminar la base de datos: {e}")
