#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Скрипт синхронизации виртуальных машин KVM из Zabbix в NetBox.

Логика работы:
1. Подключается к Zabbix и NetBox
2. Показывает список групп хостов Zabbix для выбора
3. Для каждого хоста получает данные о ВМ из item'ов шаблона KVM
4. Синхронизирует ВМ в NetBox (вкладка Virtual Machines у Device)
5. Если ВМ есть в Zabbix и NetBox - статус Active
6. Если ВМ есть только в NetBox - статус Offline
7. Если ВМ есть только в Zabbix - создается в NetBox

Требования: pynetbox, zabbix_utils, urllib3
"""

import os
import sys
import json
import re
import configparser
import urllib3
import pynetbox
from zabbix_utils import ZabbixAPI
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_FILE = "config_for_vms.ini"


def log(msg):
    """Выводит сообщение с временной меткой."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def load_config(path=CONFIG_FILE):
    """Загружает конфигурацию из INI-файла."""
    if not os.path.exists(path):
        # Создаем шаблон конфига если не существует
        create_config_template(path)
        raise FileNotFoundError(f"Config file '{path}' not found. Template created.")

    config = configparser.ConfigParser()
    config.read(path)

    return {
        "zabbix_url": config["ZABBIX"]["url"].strip(),
        "zabbix_token": config["ZABBIX"]["token"].strip(),
        "netbox_url": config["NETBOX"]["url"].strip(),
        "netbox_token": config["NETBOX"]["token"].strip(),
    }


def create_config_template(path):
    """Создает шаблон конфигурационного файла."""
    template = """[ZABBIX]
url = https://zabbix.example.com
; API token из Zabbix (User settings -> API tokens)
token = your_zabbix_api_token

[NETBOX]
url = https://netbox.example.com
; API token из NetBox (Admin -> API tokens)
token = your_netbox_api_token
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(template)
    log(f"Created config template: {path}")


def select_groups(zabbix_api):
    """Интерактивный выбор групп хостов."""
    log("Fetching host groups from Zabbix...")

    try:
        groups = zabbix_api.hostgroup.get(selectHosts=["hostid"], output=["groupid", "name"])
    except Exception as e:
        log(f"Error fetching groups: {e}")
        return []

    # Фильтруем пустые группы
    groups = [g for g in groups if isinstance(g, dict) and g.get("hosts")]
    groups.sort(key=lambda x: x.get("name", ""))

    print("\n" + "=" * 60)
    print("Available host groups:")
    print("=" * 60)

    for i, group in enumerate(groups, 1):
        host_count = len(group.get("hosts", []))
        name = group.get("name", "UNKNOWN")
        print(f"{i:3}. {name:<40} ({host_count} hosts)")

    print("\n0.  ALL GROUPS (process all)")
    print("=" * 60)

    while True:
        try:
            choice = input("\nEnter group numbers (comma-separated) or 0 for all: ").strip()

            if choice == "0":
                log(f"Selected: ALL groups ({len(groups)} total)")
                return groups

            indices = [int(x.strip()) for x in choice.split(",")]
            selected = [groups[i-1] for i in indices if 1 <= i <= len(groups)]

            if selected:
                total_hosts = sum(len(g.get("hosts", [])) for g in selected)
                log(f"Selected {len(selected)} groups with ~{total_hosts} hosts")
                return selected

            print("Invalid selection, try again.")

        except (ValueError, IndexError) as e:
            print(f"Invalid input: {e}. Use numbers like: 1,3,5 or 0")


def get_hosts_from_groups(zabbix_api, selected_groups):
    """Получает список хостов из выбранных групп."""
    all_hostids = set()

    for group in selected_groups:
        if not isinstance(group, dict):
            continue
        for host in group.get("hosts", []):
            if isinstance(host, dict) and host.get("hostid"):
                all_hostids.add(host["hostid"])

    if not all_hostids:
        return []

    log(f"Fetching details for {len(all_hostids)} hosts...")

    try:
        hosts = zabbix_api.host.get(
            hostids=list(all_hostids),
            output=["hostid", "name", "host", "description"]
        )
        return hosts if hosts else []
    except Exception as e:
        log(f"Error fetching hosts: {e}")
        return []


def get_vm_status_from_zabbix(zabbix_api, hostid):
    """
    Получает статусы ВМ из Zabbix.
    Использует item 'vmstatus' (ключ) и discovery 'vmstatus.name'.
    """
    vms = {}

    try:
        # Получаем master item vmstatus (сырые данные JSON)
        items = zabbix_api.item.get(
            hostids=hostid,
            search={"key_": "vmstatus"},
            output=["key_", "lastvalue"]
        )

        for item in items:
            if item.get("key_") == "vmstatus" and item.get("lastvalue"):
                try:
                    data = json.loads(item["lastvalue"])
                    if isinstance(data, dict) and "data" in data:
                        for vm in data["data"]:
                            if isinstance(vm, dict):
                                vm_name = vm.get("VMNAME")
                                status = vm.get("STATUS")
                                if vm_name:
                                    vms[vm_name] = {
                                        "status": status,
                                        "source": "zabbix"
                                    }
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log(f"Warning: Error getting VM status for host {hostid}: {e}")

    return vms


def get_vm_statistics_from_zabbix(zabbix_api, hostid):
    """
    Получает статистику ВМ (CPU, Memory) из Zabbix.
    Использует item 'vmstatistic_cpu_mem'.
    """
    vms_stats = {}

    try:
        items = zabbix_api.item.get(
            hostids=hostid,
            search={"key_": "vmstatistic_cpu_mem"},
            output=["key_", "lastvalue"]
        )

        for item in items:
            if item.get("key_") == "vmstatistic_cpu_mem" and item.get("lastvalue"):
                try:
                    data = json.loads(item["lastvalue"])
                    if isinstance(data, dict) and "data" in data:
                        for vm in data["data"]:
                            if isinstance(vm, dict):
                                vm_name = vm.get("VMNAME")
                                if vm_name:
                                    vms_stats[vm_name] = {
                                        "memory_actual": vm.get("actual"),  # bytes
                                        "memory_available": vm.get("available"),  # bytes
                                        "cpu_system": vm.get("cpu.system"),
                                        "cpu_user": vm.get("cpu.user"),
                                    }
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log(f"Warning: Error getting VM statistics for host {hostid}: {e}")

    return vms_stats


def get_vm_disks_from_zabbix(zabbix_api, hostid):
    """
    Получает информацию о дисках ВМ из Zabbix.
    Использует item 'vm_blk_discovery'.
    """
    vm_disks = {}

    try:
        items = zabbix_api.item.get(
            hostids=hostid,
            search={"key_": "vm_blk_discovery"},
            output=["key_", "lastvalue"]
        )

        for item in items:
            if item.get("key_") == "vm_blk_discovery" and item.get("lastvalue"):
                try:
                    data = json.loads(item["lastvalue"])
                    if isinstance(data, dict) and "data" in data:
                        for disk in data["data"]:
                            if isinstance(disk, dict):
                                vm_name = disk.get("VMNAME")
                                if vm_name:
                                    if vm_name not in vm_disks:
                                        vm_disks[vm_name] = []

                                    disk_info = {
                                        "device": disk.get("Device"),
                                        "source": disk.get("Source"),  # путь к файлу
                                        "target": disk.get("Target"),  # vdX, sdX
                                        "type": disk.get("Type"),  # file, block
                                        "capacity": None,  # будет заполнено отдельно
                                    }
                                    vm_disks[vm_name].append(disk_info)
                except json.JSONDecodeError:
                    continue

        # Получаем capacity для дисков из прототипов
        disk_items = zabbix_api.item.get(
            hostids=hostid,
            search={"key_": "disk.Capacity["},
            output=["key_", "lastvalue"]
        )

        for item in disk_items:
            key = item.get("key_", "")
            # Формат ключа: disk.Capacity[VMNAME,TARGET]
            match = re.match(r'disk\.Capacity\[(.+?),(.+?)\]', key)
            if match and item.get("lastvalue"):
                vm_name = match.group(1)
                target = match.group(2)

                if vm_name in vm_disks:
                    for disk in vm_disks[vm_name]:
                        if disk.get("target") == target:
                            try:
                                disk["capacity"] = int(item["lastvalue"])
                            except (ValueError, TypeError):
                                pass
                            break

    except Exception as e:
        log(f"Warning: Error getting VM disks for host {hostid}: {e}")

    return vm_disks


def get_vms_from_host(zabbix_api, host):
    """
    Получает полную информацию о ВМ с хоста KVM.
    Объединяет данные из разных item'ов.
    """
    hostid = host.get("hostid")
    hostname = host.get("name") or host.get("host")

    # Получаем разные типы данных
    vm_statuses = get_vm_status_from_zabbix(zabbix_api, hostid)
    vm_stats = get_vm_statistics_from_zabbix(zabbix_api, hostid)
    vm_disks = get_vm_disks_from_zabbix(zabbix_api, hostid)

    # Объединяем данные
    all_vm_names = set(vm_statuses.keys()) | set(vm_stats.keys()) | set(vm_disks.keys())

    vms = []
    for vm_name in all_vm_names:
        vm_data = {
            "name": vm_name,
            "host": hostname,
            "hostid": hostid,
            "status": vm_statuses.get(vm_name, {}).get("status", "unknown"),
            "memory": None,
            "disks": []
        }

        # Добавляем память (используем actual memory в байтах -> МБ)
        if vm_name in vm_stats:
            stats = vm_stats[vm_name]
            if stats.get("memory_actual"):
                try:
                    vm_data["memory"] = int(int(stats["memory_actual"]) / 1024 / 1024)  # MB
                except (ValueError, TypeError):
                    pass

        # Добавляем диски
        if vm_name in vm_disks:
            for disk in vm_disks[vm_name]:
                disk_size = None
                if disk.get("capacity"):
                    try:
                        # Capacity в байтах -> ГБ
                        disk_size = round(int(disk["capacity"]) / 1024 / 1024 / 1024, 2)
                    except (ValueError, TypeError):
                        pass

                vm_data["disks"].append({
                    "name": disk.get("target") or disk.get("device") or "disk",
                    "size": disk_size,  # GB
                    "path": disk.get("source"),
                    "type": disk.get("type")
                })

        vms.append(vm_data)

    return vms


def find_device_in_netbox(netbox_api, hostname):
    """Ищет device в NetBox по имени (без домена)."""
    try:
        # Нормализуем имя
        clean_name = hostname.split('.')[0].lower()

        # Ищем точное совпадение
        devices = list(netbox_api.dcim.devices.filter(name__ic=clean_name))

        for device in devices:
            if device.name and device.name.split('.')[0].lower() == clean_name:
                return device

        return None
    except Exception as e:
        log(f"Error searching device {hostname} in NetBox: {e}")
        return None


def get_or_create_cluster(netbox_api, device):
    """
    Получает или создает кластер для устройства.
    Кластер называется как устройство (KVM хост).
    """
    cluster_name = f"KVM-{device.name}"

    try:
        # Ищем существующий кластер
        clusters = list(netbox_api.virtualization.clusters.filter(name=cluster_name))
        if clusters:
            return clusters[0]

        # Ищем тип кластера KVM или создаем
        cluster_types = list(netbox_api.virtualization.cluster_types.filter(name="KVM"))
        if cluster_types:
            cluster_type = cluster_types[0]
        else:
            cluster_type = netbox_api.virtualization.cluster_types.create(
                name="KVM",
                slug="kvm",
                description="KVM Virtualization Cluster"
            )
            log(f"Created cluster type: KVM")

        # Создаем кластер
        cluster = netbox_api.virtualization.clusters.create(
            name=cluster_name,
            type=cluster_type.id,
            site=device.site.id if device.site else None,
            comments=f"Auto-created from Zabbix sync for device {device.name}"
        )
        log(f"Created cluster: {cluster_name}")
        return cluster

    except Exception as e:
        log(f"Error with cluster for {device.name}: {e}")
        return None


def get_or_create_vm_role(netbox_api):
    """Получает или создает роль 'VM' для виртуальных машин."""
    try:
        roles = list(netbox_api.dcim.device_roles.filter(name="VM"))
        if roles:
            return roles[0]

        # Создаем роль
        role = netbox_api.dcim.device_roles.create(
            name="VM",
            slug="vm",
            color="9e9e9e",  # серый цвет
            vm_role=True,
            description="Virtual Machine"
        )
        log("Created device role: VM")
        return role

    except Exception as e:
        log(f"Error creating VM role: {e}")
        return None


def sync_vm_to_netbox(netbox_api, vm_data, device, cluster, role, dry_run=False):
    """
    Синхронизирует одну ВМ в NetBox.

    Args:
        vm_data: dict с данными ВМ из Zabbix
        device: объект device NetBox (KVM хост)
        cluster: объект cluster NetBox
        role: объект device_role NetBox
        dry_run: если True, только показывает что будет сделано

    Returns:
        tuple: (action, vm_object) - действие и объект ВМ
    """
    vm_name = vm_data["name"]

    try:
        # Ищем существующую ВМ
        existing_vms = list(netbox_api.virtualization.virtual_machines.filter(name=vm_name))
        existing_vm = None

        for evm in existing_vms:
            # Проверяем что это та же ВМ (по кластеру)
            if evm.cluster and evm.cluster.id == cluster.id:
                existing_vm = evm
                break

        # Определяем статус
        zabbix_status = vm_data.get("status", "unknown")
        is_running = zabbix_status == "running"

        # Подготавливаем данные
        vm_payload = {
            "name": vm_name,
            "cluster": cluster.id,
            "role": role.id if role else None,
            "site": device.site.id if device.site else None,
            "tenant": device.tenant.id if device.tenant else None,
        }

        # Добавляем память если есть
        if vm_data.get("memory"):
            vm_payload["memory"] = vm_data["memory"]  # MB

        # Статус
        if is_running:
            vm_payload["status"] = "active"  # Active
        else:
            vm_payload["status"] = "offline"  # Offline

        # Комментарий
        comments = []
        comments.append(f"KVM Host: {device.name}")
        comments.append(f"Zabbix Status: {zabbix_status}")
        if vm_data.get("disks"):
            comments.append(f"Disks: {len(vm_data['disks'])}")
        vm_payload["comments"] = "\n".join(comments)

        if dry_run:
            if existing_vm:
                return ("UPDATE", vm_payload)
            else:
                return ("CREATE", vm_payload)

        if existing_vm:
            # Обновляем существующую
            for key, value in vm_payload.items():
                setattr(existing_vm, key, value)
            existing_vm.save()

            # Синхронизируем диски
            sync_vm_disks(netbox_api, existing_vm, vm_data.get("disks", []))

            return ("UPDATED", existing_vm)
        else:
            # Создаем новую
            new_vm = netbox_api.virtualization.virtual_machines.create(vm_payload)

            # Синхронизируем диски
            sync_vm_disks(netbox_api, new_vm, vm_data.get("disks", []))

            return ("CREATED", new_vm)

    except Exception as e:
        log(f"Error syncing VM {vm_name}: {e}")
        return ("ERROR", str(e))


def sync_vm_disks(netbox_api, vm, disks_data):
    """
    Синхронизирует диски ВМ в NetBox.
    """
    if not disks_data:
        return

    try:
        # Получаем существующие диски
        existing_disks = list(netbox_api.virtualization.virtual_disks.filter(virtual_machine_id=vm.id))
        existing_by_name = {d.name: d for d in existing_disks}

        processed_names = set()

        for disk_data in disks_data:
            disk_name = disk_data.get("name") or "disk"
            # Делаем имя уникальным если нужно
            if disk_name in processed_names:
                disk_name = f"{disk_name}_{len(processed_names)}"
            processed_names.add(disk_name)

            disk_size = disk_data.get("size")  # GB

            if disk_name in existing_by_name:
                # Обновляем существующий
                existing_disk = existing_by_name[disk_name]
                if disk_size and existing_disk.size != int(disk_size * 1024):  # NetBox хранит в МБ
                    existing_disk.size = int(disk_size * 1024)
                    existing_disk.description = f"Path: {disk_data.get('path', 'N/A')}"
                    existing_disk.save()
            else:
                # Создаем новый
                if disk_size:
                    netbox_api.virtualization.virtual_disks.create(
                        virtual_machine=vm.id,
                        name=disk_name,
                        size=int(disk_size * 1024),  # MB
                        description=f"Path: {disk_data.get('path', 'N/A')}"
                    )

        # Удаляем диски которых нет в Zabbix (опционально, пока не удаляем)

    except Exception as e:
        log(f"Error syncing disks for VM {vm.name}: {e}")


def mark_missing_vms_offline(netbox_api, cluster, current_vm_names, dry_run=False):
    """
    Помечает ВМ в NetBox как offline, если их нет в текущем списке из Zabbix.
    """
    try:
        cluster_vms = list(netbox_api.virtualization.virtual_machines.filter(cluster_id=cluster.id))

        marked = 0
        for vm in cluster_vms:
            if vm.name not in current_vm_names and vm.status.value == "active":
                if not dry_run:
                    vm.status = "offline"
                    vm.comments = (vm.comments or "") + f"\n\n[Auto] Marked offline at {datetime.now().isoformat()} - not found in Zabbix"
                    vm.save()
                marked += 1

        return marked
    except Exception as e:
        log(f"Error marking offline VMs: {e}")
        return 0


def main():
    log("Starting KVM VM synchronization...")

    # Загружаем конфиг
    try:
        cfg = load_config()
    except FileNotFoundError as e:
        log(str(e))
        sys.exit(1)

    # Подключаемся к Zabbix
    log("Connecting to Zabbix...")
    try:
        zabbix_api = ZabbixAPI(cfg["zabbix_url"])
        zabbix_api.login(cfg["zabbix_token"])
        log("Zabbix: Connected")
    except Exception as e:
        log(f"Failed to connect to Zabbix: {e}")
        sys.exit(1)

    # Подключаемся к NetBox
    log("Connecting to NetBox...")
    try:
        netbox_api = pynetbox.api(cfg["netbox_url"], cfg["netbox_token"])
        netbox_api.http_session.verify = False
        log("NetBox: Connected")
    except Exception as e:
        log(f"Failed to connect to NetBox: {e}")
        sys.exit(1)

    # Выбираем группы
    selected_groups = select_groups(zabbix_api)
    if not selected_groups:
        log("No groups selected, exiting")
        sys.exit(0)

    # Получаем хосты
    hosts = get_hosts_from_groups(zabbix_api, selected_groups)
    if not hosts:
        log("No hosts found in selected groups")
        sys.exit(0)

    log(f"Processing {len(hosts)} hosts for VM data...")

    # Собираем все ВМ из Zabbix
    all_vms = []
    for idx, host in enumerate(hosts, 1):
        hostname = host.get("name") or host.get("host")
        print(f"\rProcessing host {idx}/{len(hosts)}: {hostname:<30}", end="", flush=True)

        try:
            host_vms = get_vms_from_host(zabbix_api, host)
            all_vms.extend(host_vms)
        except Exception as e:
            log(f"\nError processing host {hostname}: {e}")

    print()
    log(f"Found {len(all_vms)} VMs in Zabbix")

    if not all_vms:
        log("No VMs found, exiting")
        sys.exit(0)

    # Группируем ВМ по хостам
    vms_by_host = {}
    for vm in all_vms:
        host = vm["host"]
        if host not in vms_by_host:
            vms_by_host[host] = []
        vms_by_host[host].append(vm)

    # Статистика
    stats = {
        "hosts_processed": 0,
        "vms_created": 0,
        "vms_updated": 0,
        "vms_marked_offline": 0,
        "errors": 0,
        "hosts_not_found": []
    }

    # Получаем или создаем роль VM
    vm_role = get_or_create_vm_role(netbox_api)

    # Обрабатываем каждый хост
    log("Synchronizing VMs to NetBox...")

    for hostname, vms in vms_by_host.items():
        # Ищем device в NetBox
        device = find_device_in_netbox(netbox_api, hostname)

        if not device:
            log(f"Warning: Device '{hostname}' not found in NetBox, skipping {len(vms)} VMs")
            stats["hosts_not_found"].append(hostname)
            continue

        # Получаем или создаем кластер
        cluster = get_or_create_cluster(netbox_api, device)
        if not cluster:
            log(f"Warning: Could not get/create cluster for {hostname}")
            continue

        stats["hosts_processed"] += 1
        current_vm_names = set()

        for vm_data in vms:
            vm_name = vm_data["name"]
            current_vm_names.add(vm_name)

            action, result = sync_vm_to_netbox(
                netbox_api, vm_data, device, cluster, vm_role, dry_run=False
            )

            if action == "CREATED":
                stats["vms_created"] += 1
                print(f"  [+] {vm_name} (on {hostname})")
            elif action == "UPDATED":
                stats["vms_updated"] += 1
                print(f"  [=] {vm_name} (on {hostname})")
            elif action == "ERROR":
                stats["errors"] += 1
                print(f"  [!] {vm_name} ERROR: {result}")

        # Помечаем отсутствующие ВМ как offline
        marked = mark_missing_vms_offline(netbox_api, cluster, current_vm_names)
        stats["vms_marked_offline"] += marked

    # Итоги
    print("\n" + "=" * 60)
    print("SYNCHRONIZATION COMPLETE")
    print("=" * 60)
    print(f"Hosts processed:     {stats['hosts_processed']}")
    print(f"VMs created:         {stats['vms_created']}")
    print(f"VMs updated:         {stats['vms_updated']}")
    print(f"VMs marked offline:  {stats['vms_marked_offline']}")
    print(f"Errors:              {stats['errors']}")

    if stats["hosts_not_found"]:
        print(f"\nHosts not found in NetBox ({len(stats['hosts_not_found'])}):")
        for h in stats["hosts_not_found"][:10]:
            print(f"  - {h}")
        if len(stats["hosts_not_found"]) > 10:
            print(f"  ... and {len(stats['hosts_not_found']) - 10} more")

    log("Done!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        log(f"UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
