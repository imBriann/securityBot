import os
import shutil

# Ruta de la base de datos (ajusta si está en otro directorio)
db_path = "usuarios_bot.db"
# Ruta del directorio de imágenes
images_dir = "imagenes_recibidas"

try:
    # Eliminar la base de datos
    if os.path.exists(db_path):
        os.remove(db_path)
        print("✅ Base de datos eliminada correctamente.")
    else:
        print("⚠️ La base de datos no existe.")
    
    # Eliminar el directorio de imágenes y su contenido
    if os.path.exists(images_dir):
        shutil.rmtree(images_dir)
        print("✅ Directorio de imágenes eliminado correctamente.")
    else:
        print("⚠️ El directorio de imágenes no existe.")
except Exception as e:
    print(f"❌ Error al realizar la limpieza: {e}")
