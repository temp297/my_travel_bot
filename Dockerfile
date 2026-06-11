# Використовуємо офіційний образ від розробників Playwright, де вже встановлено правильний Python та ВСІ системні бібліотеки Linux для браузера
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Визначаємо робочу папку в контейнері
WORKDIR /app

# Спочатку копіюємо requirements.txt для швидкого кешування залежностей
COPY requirements.txt .

# Оновлюємо pip та встановлюємо всі ваші бібліотеки (зокрема aiogram, playwright тощо)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade aiogram

# Копіюємо весь інший код вашого бота в контейнер
COPY . .

# Команда запуску бота (якщо ваш головний файл називається НЕ main.py, замініть його назву тут)
CMD ["python", "main.py"]
