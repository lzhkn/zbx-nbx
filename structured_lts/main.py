#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# main.py — интерактивный запуск синхронизации Zabbix → NetBox
# Запускает один из четырёх скриптов синхронизации по выбору пользователя.

from common import init_resources, select_groups, select_missing_vm_behavior, loging

import sync_inventory
import sync_hardware
import sync_vm_pve
import sync_vm_kvm


def select_mode():
    """Меню выбора режима синхронизации."""
    print("\n" + "=" * 50)
    print("  Что синхронизируем?")
    print("=" * 50)
    print("  1. Устройства    (serial, platform, tags, comments)")
    print("  2. Диски         (inventory items: serial, model, status)")
    print("  3. VM Proxmox    (QEMU + LXC → NetBox)")
    print("  4. VM KVM        (KVM → NetBox через Zabbix items)")
    print("=" * 50)

    while True:
        choice = input("Выберите режим [1/2/3/4]: ").strip()
        if choice in ("1", "2", "3", "4"):
            return choice
        print("  [!] Введите 1, 2, 3 или 4")


def main():
    # --- Шаг 1: проверка подключения к NetBox ---
    print("\n[*] Проверяю подключение к NetBox (тег + роль)...")
    if not init_resources():
        print("[!] Не удалось инициализировать ресурсы NetBox, см. error лог.")
        return
    print("[✓] NetBox: OK")

    # --- Шаг 2: выбор режима ---
    mode = select_mode()

    # --- Режим 1: устройства ---
    if mode == "1":
        print("\n  [Шаг: выбор групп Zabbix]")
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

        _print_summary(mode="устройства", groups=groups)
        if not _confirm():
            return

        loging(f"Start: mode=inventory | groups={len(groups)}", "sync")
        sync_inventory.run(groups=groups)

    # --- Режим 2: диски ---
    elif mode == "2":
        print("\n  [Шаг: выбор групп Zabbix]")
        groups = select_groups()
        if not groups:
            print("[!] Группы не выбраны, выход.")
            return

        _print_summary(mode="диски", groups=groups)
        if not _confirm():
            return

        loging(f"Start: mode=hardware | groups={len(groups)}", "sync")
        sync_hardware.run(groups=groups)

    # --- Режим 3: VM Proxmox ---
    elif mode == "3":
        missing_vm_behavior = select_missing_vm_behavior()

        print("\n  [Шаг: выбор групп Zabbix для фильтрации PVE-хостов (опционально)]")
        print("  Введите Enter чтобы пропустить фильтрацию по группам")
        groups = _select_groups_optional()

        _print_summary(mode="VM Proxmox", groups=groups, missing_vm=missing_vm_behavior)
        if not _confirm():
            return

        loging(f"Start: mode=vm_pve | missing_vm={missing_vm_behavior}", "sync")
        sync_vm_pve.run(groups=groups or None, missing_vm_behavior=missing_vm_behavior)

    # --- Режим 4: VM KVM ---
    elif mode == "4":
        missing_vm_behavior = select_missing_vm_behavior()

        print("\n  [Шаг: выбор групп Zabbix для фильтрации KVM-хостов (опционально)]")
        print("  Введите Enter чтобы пропустить фильтрацию по группам")
        groups = _select_groups_optional()

        _print_summary(mode="VM KVM", groups=groups, missing_vm=missing_vm_behavior)
        if not _confirm():
            return

        loging(f"Start: mode=vm_kvm | missing_vm={missing_vm_behavior}", "sync")
        sync_vm_kvm.run(groups=groups or None, missing_vm_behavior=missing_vm_behavior)


def _select_groups_optional():
    """Выбор групп Zabbix — опционально, Enter пропускает."""
    raw = input("  Выбрать группы? [y/n, Enter = нет]: ").strip().lower()
    if raw == "y":
        groups = select_groups()
        return groups if groups else []
    return []


def _print_summary(mode, groups=None, missing_vm=None):
    print(f"\n{'=' * 50}")
    print(f"  Режим:              {mode}")
    if missing_vm:
        label = "удалить" if missing_vm == "delete" else "→ offline"
        print(f"  Исчезнувшие VM:     {label}")
    if groups:
        print(f"  Групп:              {len(groups)}  ({sum(len(g['hosts']) for g in groups)} хостов)")
    print(f"{'=' * 50}")


def _confirm():
    answer = input("Запустить синхронизацию? [y/n]: ").strip().lower()
    if answer != "y":
        print("Отменено.")
        return False
    return True


if __name__ == "__main__":
    main()
