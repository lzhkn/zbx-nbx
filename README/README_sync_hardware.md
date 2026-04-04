# sync_hardware.py

Синхронизация физических дисков серверов **Zabbix → NetBox** как inventory items.

## Область применения

Обрабатывает хосты с шаблонами `Linux by Zabbix agent` и `Proxmox VE by HTTP`. Диски ищутся через Zabbix items двух типов: SMART и LSI (аппаратный RAID).

## Что синхронизируется

Каждый диск представляется в NetBox как `inventory_item` с ролью `Disks`.

| Поле NetBox | Источник в Zabbix |
|---|---|
| `name` | Имя диска из ключа item (например `sda`, `0:1:0`) |
| `serial` | Значение item `smart.disk.sn[*]` или `lsi.pd.sn[*]` |
| `part_id` | Модель диска: `smart.disk.model[*]` или `lsi.pd.model[*]` |
| `status` | `active` (виден в Zabbix) / `offline` (пропал) |
| `tags` | `zbb` — добавляется при создании/обновлении, убирается при offline |
| `role` | `Disks` (inventory item role) |

## Логика работы

### Сбор дисков из Zabbix

1. Ищет items с ключами `smart.disk.sn[*]` и `lsi.pd.sn[*]` — серийные номера
2. Для каждого найденного серийника подтягивает модель через `smart.disk.model[*]` / `lsi.pd.model[*]`
3. Ключ — серийный номер диска (уникальный идентификатор)

### Синхронизация с NetBox

| Ситуация | Действие |
|---|---|
| Диск есть в Zabbix, нет в NetBox | Создать inventory item (active, тег zbb, роль Disks) |
| Диск есть в обоих | Обновить name/model/status/tags если изменились |
| Диск только в NetBox | Перевести в offline, убрать тег zbb |

Логика offline-перехода мягкая: диск не удаляется, а переводится в `status=offline` и теряет тег `zbb`. Это позволяет отслеживать историю замен дисков.

## Структура

```
sync_hardware.py
├── get_host_templates()       # Список шаблонов хоста
├── get_item_value()           # Значение Zabbix item по ключу
├── extract_disk_name()        # Извлечение имени диска из ключа item
├── get_disk_model()           # Модель диска (SMART или LSI)
├── get_disks_from_zabbix()    # Сбор всех дисков хоста
├── get_disks_from_netbox()    # Существующие inventory items устройства
├── sync_disks()               # Синхронизация дисков одного устройства
├── sync_device_disks()        # Обёртка: находит device + запускает sync_disks
└── run()                      # Точка входа: цикл по группам и хостам
```

## Источники данных в Zabbix

| Тип | Item ключ (serial) | Item ключ (model) | Описание |
|---|---|---|---|
| SMART | `smart.disk.sn[DISK]` | `smart.disk.model[DISK]` | Прямой доступ к диску через S.M.A.R.T. |
| LSI | `lsi.pd.sn[DISK]` | `lsi.pd.model[DISK]` | Диски за аппаратным RAID-контроллером LSI/Broadcom |

## Запуск

```bash
python main.py          # режим 2
python sync_hardware.py  # напрямую
```

## Зависимости из common.py

```
cfg, zabbix_api, netbox_api,
ZABBIX_TAG, DISKS_ROLE,
loging,
select_groups, init_resources
```
