# main.py

Интерактивная точка входа проекта **zbx-nbx**. Предоставляет меню выбора режима синхронизации и запускает соответствующий модуль.

## Режимы

| # | Режим | Модуль | Что делает |
|---|---|---|---|
| 1 | Устройства | `sync_inventory` | serial, platform, tags, comments |
| 2 | Диски | `sync_hardware` | inventory items: serial, model, status |
| 3 | VM Proxmox | `sync_vm_pve` | QEMU + LXC → NetBox (прямое подключение к PVE API) |
| 4 | VM KVM | `sync_vm_kvm` | KVM → NetBox через Zabbix items |
| 5 | Сетевые | `sync_network` | serial, interfaces, descriptions сетевых устройств |

## Порядок работы

1. **Проверка подключения** — вызывает `init_resources()` из `common.py`, создаёт тег `zbb` и роль `Disks` в NetBox
2. **Выбор режима** — интерактивное меню (1–5)
3. **Выбор групп Zabbix** — glob-фильтр + номера. Для VM-режимов (3, 4) дополнительно спрашивает политику обработки исчезнувших VM (`delete` / `offline`)
4. **Подтверждение** — выводит сводку (режим, количество групп/хостов, параметры) и ждёт `y`
5. **Запуск** — вызывает `run()` выбранного модуля с передачей групп и параметров

## Запуск

```bash
python main.py
```

Каждый модуль синхронизации также может запускаться напрямую (`python sync_inventory.py` и т.д.) — в этом случае он сам вызывает `init_resources()` и `select_groups()`.

## Зависимости

Импортирует из `common.py`: `init_resources`, `select_groups`, `select_missing_vm_behavior`, `loging`.

Импортирует модули: `sync_inventory`, `sync_hardware`, `sync_vm_pve`, `sync_vm_kvm`, `sync_network`.
