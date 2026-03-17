# zabbix_netbox_sync v10

Синхронизация данных из **Zabbix** в **NetBox**.  
Запуск интерактивный — режим и объекты выбираются через меню в терминале.

---

## Содержание

1. [Режимы работы](#режимы-работы)
2. [Требования](#требования)
3. [Установка](#установка)
4. [Конфигурация](#конфигурация)
5. [Запуск](#запуск)
6. [Режим 1 — Устройства](#режим-1--устройства)
7. [Режим 2 — Диски](#режим-2--диски)
8. [Режим 3 — VM Proxmox (PVE)](#режим-3--vm-proxmox-pve)
9. [Режим 4 — VM KVM](#режим-4--vm-kvm)
10. [Тег zbb](#тег-zbb)
11. [ZBX-блок в comments](#zbx-блок-в-comments)
12. [Статусы](#статусы)
13. [Имена объектов в NetBox](#имена-объектов-в-netbox)
14. [Лог-файлы](#лог-файлы)
15. [Шаблоны Zabbix](#шаблоны-zabbix)
16. [Макросы Zabbix для PVE](#макросы-zabbix-для-pve)
17. [KVM: items шаблона](#kvm-items-шаблона)
18. [Частые проблемы](#частые-проблемы)

---

## Режимы работы

| # | Режим | Что делает |
|---|---|---|
| 1 | Устройства | serial, platform, тег zbb, zbx-блок в comments |
| 2 | Диски | inventory items: serial, модель, статус active/offline |
| 3 | VM Proxmox | QEMU VM и LXC контейнеры → NetBox (через ProxmoxAPI) |
| 4 | VM KVM | KVM виртуалки → NetBox (через Zabbix items шаблона) |
| 5 | Всё | режимы 1 + 2 + 3 + 4 последовательно |

---

## Требования

- Python 3.8+
- NetBox 3.7+ (нужен `dcim.mac_addresses`)
- Zabbix 6.0+ (API token-аутентификация)
- Proxmox VE 7+ (только для режима 3)

```bash
pip install pynetbox zabbix_utils proxmoxer urllib3
```

---

## Установка

```bash
git clone <repo>
cd zabbix_netbox_sync
pip install pynetbox zabbix_utils proxmoxer urllib3
cp config_disk.ini.example config_disk.ini
nano config_disk.ini
```

---

## Конфигурация

Файл `config_disk.ini` должен лежать рядом со скриптом.

### Обязательные секции

```ini
[ZABBIX]
url   = https://zabbix.example.com
token = your_zabbix_api_token

[NETBOX]
url   = https://netbox.example.com
token = your_netbox_api_token
```

### [PROXMOX] — для режима 3

```ini
[PROXMOX]
template_id = 10517        ; ID шаблона "Proxmox VE by HTTP" в Zabbix
role_vm     = 76           ; ID роли VM в NetBox (Virtualization → Roles)
domain      = .example.com ; домен для поиска ноды в NetBox (можно оставить пустым)
```

**template_id** — Zabbix → Configuration → Templates → "Proxmox VE by HTTP" → ID в URL.

**role_vm** — NetBox → Virtualization → Roles → нужная роль → ID в URL.

**domain** — используется при поиске физической ноды в NetBox.
Скрипт ищет device сначала по короткому имени (`pve01`), потом по `pve01<domain>` (`pve01.example.com`).
Если домен не нужен — оставьте пустым: `domain =`

### [KVM] — для режима 4

```ini
[KVM]
template_id = 11301   ; ID шаблона "Tempalte KVM" в Zabbix
role_vm     = 76      ; ID роли VM в NetBox
cluster     = KVM     ; имя кластера в NetBox (создаётся автоматически)
```

**cluster** — все KVM-гипервизоры, выбранные при запуске, попадут в один кластер с этим именем.

---

## Запуск

```bash
python3 zabbix_netbox_sync_v10.py
```

Скрипт запрашивает всё интерактивно: режим → группы/кластеры → подтверждение.

### Выбор групп Zabbix

Поддерживаются glob-паттерны через запятую:

```
Паттерны [Enter / 'all' = все группы]: Servers/*, Linux/Prod*
```

Затем из найденных групп выбираем номера или `all`:

```
  1. Linux/Prod  (12 хостов)
  2. Servers/DB  (5 хостов)

Выбор: 1,2
```

---

## Режим 1 — Устройства

Обрабатывает хосты Zabbix с шаблонами:
- `Linux by Zabbix agent` — физические серверы
- `Proxmox VE by HTTP` — PVE-гипервизоры как устройства

Для каждого хоста находит device в NetBox по **короткому имени** (без домена).
Если device не найден — пропускает с записью в `error.log`.

**Что обновляется:**

| Поле NetBox | Источник Zabbix | Условие |
|---|---|---|
| `serial` | item `dmidecode.SerialNumber` → fallback `inventory.serialno_a` | только если изменился |
| `platform` | item `os.system.product_name` → fallback `inventory.system` | только если изменилась |
| тег `zbb` | — | только если отсутствует |
| `comments` (zbx-блок) | поле `description` хоста | только если изменился текст |

---

## Режим 2 — Диски

Синхронизирует физические диски как `dcim.inventory_items`.

**Источники в Zabbix:**

| Ключ item | Что читает |
|---|---|
| `smart.disk.sn[<dev>]` | серийник (SMART — SSD/HDD) |
| `lsi.pd.sn[<dev>]` | серийник (LSI RAID, Physical Drive) |
| `smart.disk.model[<dev>]` | модель диска |
| `lsi.pd.model[<dev>]` | модель диска (LSI) |

**Логика:**

```
Диск есть в Zabbix:
  ├── есть в NetBox → проверяем status/tags/role/name/model, обновляем изменившееся
  └── нет в NetBox → создаём: status=active, тег=zbb, role=Disks

Диск только в NetBox (исчез из Zabbix):
  └── status → offline  (не удаляем, сохраняем историю)
```

---

## Режим 3 — VM Proxmox (PVE)

Подключается к Proxmox VE API напрямую. Credentials берёт из макросов хоста в Zabbix.
Шаблон `Proxmox VE by HTTP` нужен **только на одной ноде** — через неё скрипт видит весь кластер.

### Кластер vs standalone

Скрипт автоматически определяет тип через `cluster.status`:

- Есть запись `type=cluster` → **PVE-кластер**: обходятся **все ноды** через единственную точку входа, все ноды привязываются к кластеру в NetBox.
- Только `type=node` → **standalone**: обходятся только выбранные ноды.

### Что синхронизируется (QEMU VM и LXC)

| Поле NetBox | Источник PVE |
|---|---|
| `name` | `<node>/<vmid>/<vmname>` |
| `status` | статус VM (см. раздел [Статусы](#статусы)) |
| `vcpus` | `cores` из конфига VM |
| `memory` | `memory` из конфига VM (МБ) |
| `role` | из config `[PROXMOX] role_vm` |
| `device` | физическая нода (ищется в NetBox) |
| `tags` | теги из конфига VM в PVE (разделитель `;`) + тег `zbb` |
| `comments` | `description` из конфига VM (без маркеров) |
| `virtual_disks` | scsi*/ide* (QEMU) или rootfs/mp* (LXC) |
| interfaces + MAC | net0, net1, ... |

**Удаление VM:** если VM исчезла из PVE — удаляется из NetBox.
Для кластера удаляются VM по всем нодам, для standalone — только по обработанным.

---

## Режим 4 — VM KVM

Читает данные через Zabbix items шаблона `Tempalte KVM`.
Прямого подключения к гипервизору не требуется.
Шаблон нужен на **каждом гипервизоре** отдельно.

**Что синхронизируется:**

| Поле NetBox | Источник |
|---|---|
| `name` | `<node>/<VMNAME>` |
| `status` | `vmstatus.status[VMNAME]` |
| `vcpus` | `vmstatistic_cpu_mem` (JSON) |
| `memory` | `vmstatistic_cpu_mem` (JSON, байты → МБ) |
| `role` | из config `[KVM] role_vm` |
| `device` | гипервизор (ищется в NetBox) |
| тег `zbb` | добавляется при создании и обновлении |
| `virtual_disks` | `vm_blk_discovery` (JSON) |
| interfaces + MAC | `vmlist_network` (JSON) |

**Удаление VM:** удаляются только VM с гипервизоров, которые обрабатывались в текущем запуске.

---

## Тег zbb

Тег `zbb` (зелёный) проставляется на все объекты из Zabbix:
- `dcim.devices` — физические устройства
- `dcim.inventory_items` — диски
- `virtualization.virtual_machines` — все VM (PVE и KVM)

Тег только добавляется, никогда не удаляется. Другие теги объекта не трогаются.
Создаётся автоматически при первом запуске если не существует.

---

## ZBX-блок в comments

Только для **физических устройств** (`dcim.devices`).

Поле `description` хоста из Zabbix записывается в `comments` NetBox внутри маркеров:

```
== zabbix description ==

Критичность: Критично 24/7. Владелец: Иванов И.И.
Описание: Сервер виртуализации KVM.

== zabbix description ==
```

**Зачем маркеры:** оператор может писать свои заметки в `comments` выше или ниже блока — при следующей синхронизации они не будут затронуты. Обновляется только содержимое между маркерами.

**Для VM (PVE и KVM)** — маркеры не используются. `comments` пишется как есть (чистый текст из `description`).

---

## Статусы

### Диски (inventory_items)

| Ситуация | Статус |
|---|---|
| Диск виден в Zabbix | `active` |
| Диск исчез из Zabbix | `offline` |

### VM Proxmox

| Статус PVE | Статус NetBox |
|---|---|
| `running` | `active` |
| `stopped` | `offline` |
| `paused` | `planned` |
| VM является шаблоном | `staged` |
| Другой | `failed` |

### VM KVM

| Статус KVM | Статус NetBox |
|---|---|
| `running` | `active` |
| `shut off` / `shut` / `shutdown` / `in shutdown` | `offline` |
| `paused` | `planned` |
| `idle` | `planned` |
| `pmsuspended` | `planned` |
| `crashed` | `failed` |
| Неизвестный | `offline` |

---

## Имена объектов в NetBox

| Объект | Формат | Пример |
|---|---|---|
| Device | короткое имя хоста (без домена) | `server01` |
| Inventory item (диск) | имя устройства из Zabbix | `sda`, `pd:0:2` |
| PVE QEMU VM | `<node>/<vmid>/<vmname>` | `pve01/100/web-server` |
| PVE LXC | `<node>/<vmid>/<ctname>` | `pve01/200/nginx-ct` |
| KVM VM | `<node>/<VMNAME>` | `kvm01/db-master` |

---

## Лог-файлы

Создаются рядом со скриптом, новый файл каждый день.

| Файл | Содержимое |
|---|---|
| `sync_YYYY-MM-DD.log` | создания, обновления, удаления, привязки |
| `error_YYYY-MM-DD.log` | ошибки API, не найденные устройства, таймауты |
| `debug_YYYY-MM-DD.log` | пропущенные объекты (no changes), пустые items |

Формат строки:
```
14:32:05 :: [server01] Device updated: serial: ∅ → SN123456,  tag: +zbb
14:32:06 :: [server01] Disk created: ABC123, model=Samsung 870 EVO
14:32:10 :: [pve01/100/web-server] VM created
```

---

## Шаблоны Zabbix

### Linux by Zabbix agent

Для режимов 1 и 2.

| Ключ item | Назначение |
|---|---|
| `dmidecode.SerialNumber` | серийный номер сервера (приоритет) |
| `os.system.product_name` | модель сервера / платформа (приоритет) |
| `smart.disk.sn[<dev>]` | серийник диска (SMART) |
| `smart.disk.model[<dev>]` | модель диска (SMART) |
| `lsi.pd.sn[<dev>]` | серийник диска (LSI RAID) |
| `lsi.pd.model[<dev>]` | модель диска (LSI RAID) |

Если item отсутствует или пустой — fallback на поля inventory хоста (`serialno_a`, `system`).

### Proxmox VE by HTTP

Для режимов 1, 2 (гипервизор как устройство) и режима 3 (источник credentials).
Шаблон нужен **только на одной ноде кластера** — обычно на первой или управляющей.

---

## Макросы Zabbix для PVE

Credentials для ProxmoxAPI берутся из макросов хоста в Zabbix.
Макросы хоста перекрывают макросы шаблона.

| Макрос | Содержимое | Пример |
|---|---|---|
| `{$PVE.URL.HOST}` | IP или DNS ноды | `192.168.1.10` |
| `{$PVE.URL.PORT}` | порт API | `8006` |
| `{$PVE.TOKEN.ID}` | `user@realm!tokenname` | `root@pam!zabbix-sync` |
| `{$PVE.TOKEN.SECRET}` | секрет токена | `xxxxxxxx-xxxx-...` |

Формат `{$PVE.TOKEN.ID}`: разделитель `!` между `user@realm` и именем токена.

**Как создать токен в PVE:**
Datacenter → Permissions → API Tokens → Add.
Минимальные права: `VM.Audit`, `Datastore.Audit`, `Sys.Audit` (read-only).

---

## KVM: items шаблона

Шаблон `Tempalte KVM` должен быть привязан к каждому гипервизору в Zabbix.

### Master items (RAW JSON, обновляются каждые 30 минут)

| Ключ | Структура |
|---|---|
| `vmstatistic_cpu_mem` | `{"data": [{"VMNAME": "...", "actual": <bytes>, "nrVirtCpu": N}]}` |
| `vm_blk_discovery` | `{"data": [{"VMNAME": "...", "Target": "vda", "Source": "/path/img", "Device": "disk"}]}` |
| `vmlist_network` | `{"data": [{"VMNAME": "...", "Interface": "vnet0", "MAC": "aa:bb:..."}]}` |

### Dependent items (создаются LLD, по одному на каждую VM)

| Ключ | Содержимое |
|---|---|
| `vmstatus.status[<VMNAME>]` | статус: `running`, `shut off`, `paused`, `crashed` |
| `disk.Capacity[<VMNAME>,<target>]` | размер диска в байтах |

Если LLD discovery ещё не отработал (шаблон только что привязали) — dependent items будут отсутствовать. Скрипт выведет предупреждение и пропустит гипервизор. Нужно подождать цикл discovery (по умолчанию 1 час) или запустить вручную в Zabbix.

---

## Частые проблемы

### Device not found in NetBox

```
error :: [server01.example.com] Device not found in NetBox
```

Скрипт ищет device по **короткому имени** без домена: `server01.example.com` → ищет `server01`.
Убедитесь что device в NetBox называется `server01`.

### PVE: таймаут подключения

```
[!] Не удалось получить статус кластера (таймаут или недоступен)
[!] Кластер sh-pve-01 пропущен.
```

Скрипт пропускает недоступный кластер и продолжает со следующим.
Проверьте доступность `{$PVE.URL.HOST}:{$PVE.URL.PORT}` с машины где запускается скрипт.

### PVE: VM с других нод кластера не видны

Убедитесь что токен PVE имеет права на уровне **Datacenter** (не отдельной ноды).
Скрипт определяет кластер автоматически и обходит все ноды через точку входа — шаблон нужен только на одной.

### KVM: нет items vmstatus.status

```
[!] Не найдено ни одного item vmstatus.status[*] на kvm01
```

Причины:
1. Шаблон `Tempalte KVM` не привязан к гипервизору
2. LLD discovery ещё не запускался — подождите или запустите вручную в Zabbix

### KVM: VM в статусе failed вместо offline

Zabbix может отдавать статус `shut` вместо `shut off`.
Скрипт обрабатывает все варианты: `shut`, `shut off`, `shutdown`, `in shutdown` → `offline`.
Неизвестный статус также считается `offline`.

### SSL: certificate verify failed

Проверка SSL отключена намеренно — скрипт рассчитан на self-signed сертификаты.
Предупреждения urllib3 подавляются автоматически.

### NetBox 502/503/504

Скрипт автоматически повторяет запрос 3 раза с паузой 5 секунд.
Если все попытки неудачны — объект пропускается с записью в error.log.
