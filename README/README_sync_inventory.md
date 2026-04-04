# sync_inventory.py

Синхронизация базовых данных устройств (серверов) **Zabbix → NetBox**: serial number, platform, теги, комментарии.

## Область применения

Обрабатывает хосты с шаблонами `Linux by Zabbix agent` и `Proxmox VE by HTTP`. Устройства должны уже существовать в NetBox — модуль только **обновляет**, не создаёт.

## Что синхронизируется

| Поле NetBox | Источник в Zabbix | Приоритет |
|---|---|---|
| `serial` | item `dmidecode.SerialNumber` → inventory `serialno_a` | item первый, inventory fallback |
| `platform` | item `os.system.product_name` → inventory `system` | item первый, inventory fallback |
| `tags` | — | Добавляется тег `zbb` если отсутствует |
| `comments` | `description` хоста | Вставляется как ZBX-блок (не затирает пользовательские комментарии) |

## Логика работы

1. Для каждого хоста в выбранных группах проверяет наличие шаблона из `SYNC_TEMPLATES`
2. Собирает данные через `get_linux_host_extended()`: serial, platform, description
3. Ищет device в NetBox по короткому имени (до первой точки)
4. Сравнивает каждое поле — обновляет только изменившиеся
5. Не найденные в NetBox устройства пропускаются с записью в error-лог

## Структура

```
sync_inventory.py
├── get_host_templates()          # Список шаблонов хоста
├── get_item_value()              # Значение Zabbix item по ключу
├── get_linux_host_extended()     # Сбор данных хоста (serial, platform, description)
├── sync_device()                 # Обновление одного устройства в NetBox
└── run()                         # Точка входа: цикл по группам и хостам
```

## Запуск

```bash
python main.py        # режим 1
python sync_inventory.py   # напрямую
```

## Зависимости из common.py

```
cfg, zabbix_api, netbox_api,
ZABBIX_TAG, DISKS_ROLE,
loging, compact_text, slugify,
inject_zbx_block, extract_zbx_block_text,
get_or_create_platform,
select_groups, init_resources
```
