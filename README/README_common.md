# common.py

Общий модуль утилит, конфигурации и инициализации API. Импортируется всеми скриптами синхронизации.

## Ответственность

Модуль решает четыре задачи: загрузка конфигурации, подключение к API (Zabbix + NetBox), общие утилиты для работы с данными, и интерактивный ввод пользователя.

## Конфигурация (config.ini)

Файл `config.ini` лежит рядом со скриптами. Обязательные секции — `[ZABBIX]` и `[NETBOX]`, остальные опциональны.

```ini
[ZABBIX]
url   = https://zabbix.example.com
token = your_zabbix_api_token

[NETBOX]
url   = https://netbox.example.com
token = your_netbox_api_token

[PROXMOX]
template_id = 10517          # ID шаблона Proxmox VE в Zabbix
role_vm     = 76             # ID роли VM в NetBox
domain      = .example.com   # Домен для поиска device по FQDN

[KVM]
template_id = 11301          # ID шаблона KVM в Zabbix
role_vm     = 76             # ID роли VM в NetBox

[NETWORK]
default_role        = Network                # Fallback роль для сетевых устройств
default_site        = default                # Fallback site
default_device_type = Generic Network Device # Fallback device type
```

При отсутствии обязательных параметров (`[ZABBIX] url/token`, `[NETBOX] url/token`) скрипт завершается с выводом примера конфигурации.

## Подключение к API

Подключения создаются на уровне модуля (при первом импорте):

| Переменная | Библиотека | Назначение |
|---|---|---|
| `zabbix_api` | `zabbix_utils.ZabbixAPI` | API Zabbix, авторизация по токену |
| `netbox_api` | `pynetbox.api` | API NetBox, SSL-верификация отключена |

## Глобальные ресурсы

| Переменная | Тип | Назначение |
|---|---|---|
| `ZABBIX_TAG` | pynetbox Tag | Тег `zbb` — маркер объектов, синхронизированных из Zabbix |
| `DISKS_ROLE` | pynetbox InventoryItemRole | Роль `Disks` для inventory items (физические диски) |

Инициализируются через `init_resources()`. Если создание не удалось — функция возвращает `False` и скрипт завершается.

## Утилиты

### Текст и slug

| Функция | Назначение |
|---|---|
| `slugify(text)` | Строка → NetBox slug (a-z, 0-9, -, _), максимум 50 символов |
| `compact_text(text)` | Нормализация текста: убирает пустые строки, экранирует Markdown-символы (`#`, `-`, `*`, `+`) |

### Логирование

`loging(data, namefile)` — пишет в файл `{namefile}_{YYYY-MM-DD}.log` с временной меткой. Типы файлов: `sync` (основной), `error`, `debug`.

### ZBX-блок в comments

Механизм для хранения описания из Zabbix внутри поля `comments` устройства NetBox, не затирая пользовательские комментарии.

| Функция | Назначение |
|---|---|
| `build_zbx_block(text)` | Оборачивает текст маркерами `== zabbix description ==` |
| `inject_zbx_block(comments, text)` | Вставляет или обновляет ZBX-блок в comments, остальной текст не трогает |
| `extract_zbx_block_text(comments)` | Извлекает содержимое ZBX-блока для сравнения (без маркеров) |

Пример содержимого поля `comments` после синхронизации:

```
Какие-то пользовательские заметки...

== zabbix description ==

Сервер приложений, стойка 14-A
Ответственный: ivanov

== zabbix description ==
```

### NetBox: get-or-create

Все функции используют паттерн «найти → создать → при ошибке retry»:

| Функция | Объект NetBox |
|---|---|
| `get_or_create_tag(name, color)` | `extras.tags` |
| `get_or_create_inventory_role(name)` | `dcim.inventory_item_roles` |
| `get_or_create_platform(name)` | `dcim.platforms` |
| `get_or_create_cluster_type(name)` | `virtualization.cluster_types` |

### Retry

`netbox_call_with_retry(fn, retries=3, delay=5)` — обёртка для вызовов NetBox API. Повторяет при 502/503/504/ConnectionError/Timeout с паузой 5 секунд между попытками. Остальные ошибки пробрасывает сразу.

### Поиск устройства

`nb_find_device(name)` — ищет device в NetBox сначала по короткому имени, потом с доменом из конфига (`pve_domain`). Например: `server01` → не найден → `server01.example.com`.

## Интерактивный ввод

### select_groups()

Выбор групп Zabbix для обработки:

1. Загружает все группы из Zabbix с хостами
2. Предлагает glob-фильтр (например `Net/*`, `Servers/Prod*`; несколько через запятую)
3. Показывает нумерованный список отфильтрованных групп
4. Пользователь вводит номера через запятую или `all`

### select_missing_vm_behavior()

Спрашивает что делать с VM, которые есть в NetBox но отсутствуют на гипервизоре:

| Ответ | Поведение |
|---|---|
| `y` (delete) | Удалить VM из NetBox |
| `n` (offline) | Перевести в статус `offline` (для истории) |

### _handle_missing_vm(nb_vm, behavior)

Реализация политики: удаляет VM или переводит в offline в зависимости от выбранного поведения.

## Экспортируемые объекты

```
cfg, zabbix_api, netbox_api,
ZABBIX_TAG, DISKS_ROLE,
loging, slugify, compact_text,
inject_zbx_block, extract_zbx_block_text, build_zbx_block,
get_or_create_tag, get_or_create_platform,
get_or_create_inventory_role, get_or_create_cluster_type,
nb_find_device, netbox_call_with_retry,
select_groups, select_missing_vm_behavior,
_handle_missing_vm, init_resources,
```
