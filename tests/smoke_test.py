import urllib.request
import urllib.parse
import json
import http.cookiejar

# Используем CookieJar для сохранения сессии (как в браузере)
cookie_jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

BASE_URL = "http://localhost:80"

def get(path):
    print(f"🔍 [GET] {path}...", end=" ")
    try:
        req = urllib.request.Request(f"{BASE_URL}{path}")
        with opener.open(req) as response:
            print(f"✅ {response.status}")
            return response.read().decode("utf-8")
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return None

def post(path, data):
    print(f"📝 [POST] {path}...", end=" ")
    try:
        encoded_data = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(f"{BASE_URL}{path}", data=encoded_data)
        with opener.open(req) as response:
             print(f"✅ {response.status}")
             return response.read().decode("utf-8")
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return None

print("\n--- 🚀 НАЧАЛО ТЕСТИРОВАНИЯ ---\n")

# 1. Проверка доступности публичных страниц
print("--- Проверка страниц ---")
get("/")
get("/menu")
get("/contact")
get("/booking")

# 2. Работа с API и Корзиной
print("\n--- Проверка сценария покупки ---")
menu_json = get("/api/menu")
if menu_json:
    try:
        menu = json.loads(menu_json)
        print(f"   ℹ️ Меню загружено: найдено {len(menu)} категорий.")
        
        if menu:
            # Берем первый товар из первой категории
            cat_name = list(menu.keys())[0]
            cat_data = menu[cat_name]
            
            # Определяем, список это или подразделы
            if isinstance(cat_data, dict) and "subsections" in cat_data:
                # Если подразделы, берем первый
                sub_name = list(cat_data["subsections"].keys())[0]
                print(f"   ℹ️ Выбрана категория: {cat_name} -> {sub_name}")
                post_data = {
                    "category": cat_name,
                    "subname": sub_name,
                    "item_idx": 0,
                    "qty": 2
                }
            else:
                # Если просто список
                print(f"   ℹ️ Выбрана категория: {cat_name}")
                post_data = {
                    "category": cat_name,
                    "item_idx": 0,
                    "qty": 2
                }
            
            # Проверяем, нужны ли варианты (размер/объем)
            # В menu.json items[0] может быть dict
            try:
                # Находим сам товар в меню для проверки
                if isinstance(cat_data, dict) and "subsections" in cat_data:
                     sub_items = cat_data["subsections"][sub_name]
                     target_item = sub_items[0]
                else:
                     target_item = cat_data[0]
                
                if target_item.get("variants"):
                    print(f"   ℹ️ У товара есть варианты, выбираем первый.")
                    post_data["variant_idx"] = 0
            except Exception as ex:
                print(f"   ⚠️ Не удалось проверить варианты товара: {ex}")

            # Добавляем в корзину
            print(f"   ℹ️ Попытка добавить товар в корзину...")
            post("/cart/add", post_data)
            
            # Проверяем счетчик корзины
            count_json = get("/api/cart_count")
            if count_json:
                cnt = json.loads(count_json).get("count")
                if cnt == 2:
                    print(f"   ✅ Товар добавлен! В корзине: {cnt} шт.")
                else:
                    print(f"   ⚠️ Странное количество в корзине: {cnt} (ожидалось 2).")

    except Exception as e:
        print(f"   ❌ Ошибка обработки меню: {e}")

# 3. Проверка страницы корзины
print("\n--- Финальная проверка ---")
get("/cart")

print("\n--- 🏁 ТЕСТИРОВАНИЕ ЗАВЕРШЕНО ---")
