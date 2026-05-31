import sys
import os

# Добавляем текущую папку в путь, чтобы импорты работали корректно
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import uvicorn
    print(">>> Запуск API сервера на порту 8000...", flush=True)
    # Запускаем приложение из api.main
    uvicorn.run("api.main:app", host="127.0.0.1", port=8000, reload=False)
except ImportError as e:
    print(f"!!! Ошибка импорта: {e}")
    print("Попробуйте выполнить: pip install uvicorn fastapi python-dotenv")
except Exception as e:
    print(f"!!! Критическая ошибка: {e}")
    import traceback
    traceback.print_exc()