# zbx-nbx

Скрипт синхронизации данных из **Zabbix** в **NetBox**.

---

## Возможности

| Режим | Что синхронизируется |
|-------|----------------------|
| Устройства | serial number, platform, тег `zbb`, описание Zabbix в comments |
| Диски | inventory items (smart/LSI): серийник, модель, статус |
| VM Proxmox VE | QEMU VM и LXC: статус, vCPU, RAM, serial (vmid), диски, интерфейсы, MAC, теги |
| VM KVM | виртуальные машины: статус, vCPU, RAM, диски, интерфейсы, MAC, теги |

---

## Требования

```
python >= 3.9
pynetbox
zabbix_utils
proxmoxer
urllib3
```

Установка зависимостей:

```bash
pip install pynetbox zabbix_utils proxmoxer urllib3
```

---

## Конфигурация

Файл `config_all.ini` в той же директории что и скрипт.

```ini
[ZABBIX]
url   = https://zabbix.example.com
token = your_zabbix_api_token

[NETBOX]
url   = https://netbox.example.com
token = your_netbox_api_token

[PROXMOX]
template_id = 10517
role_vm     = 76
domain      = .example.com

[KVM]
template_id = 11301
role_vm     = 76
```

### Параметры

**[ZABBIX]**
- `url` — адрес Zabbix
- `token` — API-токен (Zabbix 5.4+)

**[NETBOX]**
- `url` — адрес NetBox
- `token` — API-токен

**[PROXMOX]** *(опционально, нужен для синхронизации PVE VM)*
- `template_id` — ID шаблона Zabbix "Proxmox VE by HTTP" (из которого берутся credentials для подключения к PVE API)
- `role_vm` — ID роли в NetBox для VM (раздел Virtualization → Roles)
- `domain` — домен для поиска device по имени ноды (например `.example.com`); если нода в NetBox называется `pve-node-01.example.com`, а в PVE — `pve-node-01`

**[KVM]** *(опционально, нужен для синхронизации KVM VM)*
- `template_id` — ID шаблона Zabbix "Template KVM"
- `role_vm` — ID роли в NetBox для VM

---

## Запуск

```bash
python zabbix_netbox_sync.py
```

Скрипт интерактивный — все параметры задаются в диалоге.

---

## Порядок работы

### 1. Проверка подключения

При старте скрипт проверяет доступность NetBox: создаёт (или находит) тег `zbb` и роль `Disks`. Если не удаётся — завершается с ошибкой.

### 2. Выбор режима синхронизации

```
1. Только устройства  (serial, platform, tags, comments)
2. Только диски       (inventory items)
3. Только VM Proxmox  (PVE QEMU + LXC → NetBox)
4. Только VM KVM      (KVM → NetBox через Zabbix items)
5. Всё                (устройства + диски + PVE VM + KVM VM)
```

### 3. Поведение при исчезнувших VM

*Спрашивается только при выборе режимов 3, 4 или 5.*

```
y — Удалить из NetBox
n — Оставить, перевести в статус offline (для истории)
```

### 4. Выбор групп / кластеров / гипервизоров

В зависимости от режима скрипт предложит:

- **Группы Zabbix** — для синхронизации устройств и дисков
- **PVE-хосты** — точки входа в Proxmox-кластеры (берутся из хостов с привязанным шаблоном)
- **KVM-гипервизоры** — хосты с шаблоном KVM

Выбор поддерживает **glob-паттерны** (`*`, `?`) и номера через запятую.

### 5. Подтверждение и запуск

Выводится сводка настроек, после подтверждения `y` начинается синхронизация.

---

## Архитектура синхронизации

### Устройства (режим 1)

Обрабатываются хосты Zabbix с шаблонами:
- `Linux by Zabbix agent`
- `Proxmox VE by HTTP`
- `Template KVM`

Что обновляется в NetBox device:
- `serial` — из item `dmidecode.SerialNumber` или inventory `serialno_a`
- `platform` — из item `os.system.product_name` или inventory `system` (создаётся автоматически)
- `tags` — добавляется тег `zbb`
- `comments` — описание хоста из Zabbix вставляется в специальный блок между маркерами `== zabbix description ==`; текст вне блока не затрагивается

### Диски (режим 2)

Источники дисков в Zabbix:
- `smart.disk.sn[*]` + `smart.disk.model[*]` — диски через smartmontools
- `lsi.pd.sn[*]` + `lsi.pd.model[*]` — диски через LSI/MegaRAID

В NetBox создаются/обновляются **inventory items** устройства:
- `name` — имя устройства (sda, sdb, ...)
- `serial` — серийный номер
- `part_id` — модель диска
- `status` — `active` если виден в Zabbix, `offline` если только в NetBox
- `role` — роль `Disks`
- `tags` — тег `zbb`

### VM Proxmox VE (режим 3)

**Кластер per-нода:** каждая PVE-нода = отдельный кластер NetBox с именем ноды (тип "Proxmox VE"). `device.cluster` ноды привязывается к этому кластеру.

**Реальный PVE-кластер:** если `cluster.status` возвращает запись с `type=cluster`, скрипт обходит все ноды через единую точку входа (из Zabbix берётся один хост на весь кластер).

**Standalone-нода:** фильтрация по выбранным нодам.

Что создаётся/обновляется в NetBox virtual machine:
- `name` — имя VM из конфига PVE
- `serial` — vmid (числовой ID виртуальной машины в PVE)
- `status` — `active` (running), `offline` (stopped), `planned` (paused), `staged` (template)
- `vcpus`, `memory`
- `device` — привязка к физической ноде
- `cluster` — кластер ноды
- `tags` — `zbb` + теги из конфига PVE (поле `tags`)
- `comments` — описание из конфига PVE
- `virtual_disks` — диски (scsi*, ide* для QEMU; rootfs, mp* для LXC)
- `interfaces` + MAC-адреса

### VM KVM (режим 4)

**Кластер per-гипервизор:** каждый KVM-хост = отдельный кластер NetBox с именем гипервизора (тип "KVM"). `device.cluster` привязывается.

Данные читаются из Zabbix items шаблона KVM:

| Item (ключ) | Тип | Содержимое |
|-------------|-----|------------|
| `vmstatus.status[VMNAME]` | dependent LLD | статус VM |
| `vmstatistic_cpu_mem` | RAW TEXT JSON | vCPU и RAM |
| `vm_blk_discovery` | RAW TEXT JSON | диски |
| `vmlist_network` | RAW TEXT JSON | сетевые интерфейсы и MAC |

Что создаётся/обновляется:
- `name` — имя VM
- `status`, `vcpus`, `memory`, `device`, `cluster`, `tags`
- `virtual_disks` — путь `target:source`, размер в MB
- `interfaces` + MAC-адреса

**Защита от пустых данных:** если Zabbix не вернул данные по дискам или интерфейсам (item ещё не собран), удаление существующих записей в NetBox не производится.

---

## Логи

Скрипт пишет три лог-файла в текущую директорию:

| Файл | Содержимое |
|------|------------|
| `sync_YYYY-MM-DD.log` | основные операции: создание, обновление, пропуск |
| `error_YYYY-MM-DD.log` | ошибки API, недоступные устройства |
| `debug_YYYY-MM-DD.log` | детальные пропуски (no changes, empty items) |

---

## Ресурсы создаваемые автоматически

При первом запуске скрипт создаёт в NetBox следующие объекты если они отсутствуют:

- **Тег** `zbb` (зелёный) — маркер объектов синхронизированных из Zabbix
- **Роль inventory items** `Disks` (синяя) — для дисков устройств
- **Платформы** — по данным из Zabbix (model/product name серверов)
- **Типы кластеров** — `Proxmox VE`, `KVM`
- **Кластеры** — по одному на каждую PVE-ноду и KVM-гипервизор

---

## Особенности и ограничения

- SSL-верификация отключена (self-signed сертификаты)
- NetBox API вызывается с retry при 502/503/504 (3 попытки, пауза 5 сек)
- Тег `zbb` только добавляется, никогда не удаляется
- Комментарии device вне ZBX-блока (`== zabbix description ==`) скриптом не изменяются
- Диски устройств (inventory items) при исчезновении переводятся в `offline`, не удаляются
- VM при исчезновении — на выбор пользователя: удалить или `offline`
- Для KVM: скрипт не имеет прямого доступа к гипервизору, все данные берутся из Zabbix items; если items не собраны — синхронизация VM пропускается без удаления данных
