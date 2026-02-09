# -*- coding: utf-8 -*-
import json
import os
import time
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

from paths import runtime_base
from ui import Fore, style_text, banner, full_line
from utils import ask, ask_int, press_enter, warn

CONVERSATION_ENGINE_FILE = runtime_base(Path(__file__).resolve().parent.parent.parent) / "storage" / "conversation_engine.json"
EXPORTS_DIR = runtime_base(Path(__file__).resolve().parent.parent.parent) / "exports"

ROLE_SALUDO = "SALUDO"
ROLE_PITCH = "PITCH"
ROLE_CTA = "CTA"
ROLE_FOLLOW_UP = "FOLLOW_UP"

ROLE_LABELS = {
    ROLE_SALUDO: "SALUDOS",
    ROLE_PITCH: "PITCH",
    ROLE_CTA: "CTA / AGENDA",
    ROLE_FOLLOW_UP: "FOLLOW-UPS"
}

METRIC_NAMES = {
    ROLE_SALUDO: "% respuesta",
    ROLE_PITCH: "% continuidad",
    ROLE_CTA: "% conversión",
    ROLE_FOLLOW_UP: "% reactivación"
}

def load_conversation_engine() -> Dict[str, Any]:
    if not CONVERSATION_ENGINE_FILE.exists():
        return {"conversations": {}}
    try:
        return json.loads(CONVERSATION_ENGINE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        warn(f"Error cargando conversation_engine.json: {e}")
        return {"conversations": {}}

def format_duration(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "0m"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

def categorize_messages(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    categorized_messages = []
    conversations = data.get("conversations", {})

    for thread_key, thread_data in conversations.items():
        messages_sent = thread_data.get("messages_sent", [])
        messages_sent_sorted = sorted(messages_sent, key=lambda m: m.get("first_sent_at", 0))

        for idx, msg in enumerate(messages_sent_sorted):
            if idx == 0:
                role = ROLE_SALUDO
            elif idx == 1:
                role = ROLE_PITCH
            elif idx == 2:
                role = ROLE_CTA
            else:
                role = ROLE_FOLLOW_UP

            msg_entry = {
                "thread_id": thread_data.get("thread_id", thread_key),
                "account": thread_data.get("account", thread_key.split("|")[0] if "|" in thread_key else "unknown"),
                "recipient": thread_data.get("recipient_username", "unknown"),
                "text": msg.get("text", ""),
                "role": role,
                "sent_at": msg.get("first_sent_at"),
                "next_sent_at": messages_sent_sorted[idx+1].get("first_sent_at") if idx+1 < len(messages_sent_sorted) else None,
                "thread_data": thread_data
            }
            categorized_messages.append(msg_entry)

    return categorized_messages

def calculate_hourly_performance(sent_data: List[Tuple[float, bool]]) -> Dict[str, float]:
    buckets = {
        "08-12": {"sent": 0, "resp": 0},
        "12-16": {"sent": 0, "resp": 0},
        "16-20": {"sent": 0, "resp": 0},
        "20-00": {"sent": 0, "resp": 0}
    }

    for ts, resp in sent_data:
        if not ts: continue
        hour = datetime.fromtimestamp(ts).hour
        b = None
        if 8 <= hour < 12: b = "08-12"
        elif 12 <= hour < 16: b = "12-16"
        elif 16 <= hour < 20: b = "16-20"
        elif 20 <= hour <= 23 or 0 <= hour < 8: b = "20-00" # Agrupamos noche en el último bucket para seguir el ejemplo

        if b:
            buckets[b]["sent"] += 1
            if resp:
                buckets[b]["resp"] += 1

    result = {}
    for b, data in buckets.items():
        if data["sent"] > 0:
            result[b] = (data["resp"] / data["sent"]) * 100
        else:
            result[b] = 0.0
    return result

def calculate_metrics(categorized_messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    raw_stats = {
        ROLE_SALUDO: defaultdict(lambda: {"sent_info": []}),
        ROLE_PITCH: defaultdict(lambda: {"sent_info": []}),
        ROLE_CTA: defaultdict(lambda: {"sent_info": []}),
        ROLE_FOLLOW_UP: defaultdict(lambda: {"sent_info": []})
    }

    for msg in categorized_messages:
        role = msg["role"]
        text = msg["text"]
        thread_data = msg["thread_data"]
        sent_at = msg["sent_at"]
        next_sent_at = msg["next_sent_at"]

        responded = False
        response_time = None

        all_msgs = thread_data.get("messages", [])
        if all_msgs:
            inbound_responses = [m for m in all_msgs if m.get("direction") == "inbound" and m.get("timestamp_epoch", 0) > sent_at]
            if next_sent_at:
                inbound_responses = [m for m in inbound_responses if m.get("timestamp_epoch", 0) < next_sent_at]

            if inbound_responses:
                inbound_responses.sort(key=lambda x: x.get("timestamp_epoch", 0))
                first_resp = inbound_responses[0]
                responded = True
                response_time = first_resp.get("timestamp_epoch") - sent_at
        else:
            last_received = thread_data.get("last_message_received_at")
            if last_received and last_received > sent_at:
                if next_sent_at is None or last_received < next_sent_at:
                    responded = True
                    response_time = last_received - sent_at

        raw_stats[role][text]["sent_info"].append((sent_at, responded, response_time))

    final_stats = {}
    for role in [ROLE_SALUDO, ROLE_PITCH, ROLE_CTA, ROLE_FOLLOW_UP]:
        role_messages = []
        total_sent = 0
        total_responded = 0
        all_role_sent_info = []
        all_response_times = []

        texts = sorted(raw_stats[role].keys(), key=lambda t: len(raw_stats[role][t]["sent_info"]), reverse=True)

        for text in texts:
            info_list = raw_stats[role][text]["sent_info"]
            sent = len(info_list)
            resp = sum(1 for _, r, _ in info_list if r)
            times = [t for _, r, t in info_list if r and t is not None]

            total_sent += sent
            total_responded += resp
            all_role_sent_info.extend([(ts, r) for ts, r, t in info_list])
            all_response_times.extend(times)

            role_messages.append({
                "text": text,
                "sent": sent,
                "responded": resp,
                "resp_rate": (resp / sent * 100) if sent > 0 else 0,
                "avg_time": sum(times) / len(times) if times else 0,
                "min_time": min(times) if times else 0,
                "max_time": max(times) if times else 0,
                "hourly_dist": calculate_hourly_performance([(ts, r) for ts, r, t in info_list])
            })

        final_stats[role] = {
            "messages": role_messages,
            "total_sent": total_sent,
            "total_responded": total_responded,
            "resp_rate": (total_responded / total_sent * 100) if total_sent > 0 else 0,
            "avg_time": sum(all_response_times) / len(all_response_times) if all_response_times else 0,
            "hourly_dist": calculate_hourly_performance(all_role_sent_info)
        }

    return final_stats

def render_main_menu(stats: Dict[str, Any]):
    while True:
        banner()
        print(style_text("ESTADÍSTICAS Y MÉTRICAS", color=Fore.CYAN, bold=True))
        print(full_line(color=Fore.BLUE))
        print(f"1) {ROLE_LABELS[ROLE_SALUDO]}")
        print(f"2) {ROLE_LABELS[ROLE_PITCH]}")
        print(f"3) {ROLE_LABELS[ROLE_CTA]}")
        print(f"4) {ROLE_LABELS[ROLE_FOLLOW_UP]}")
        print("5) Resumen general")
        print("6) Exportar datos")
        print("7) Volver")
        print(full_line(color=Fore.BLUE))

        choice = ask("Opción: ").strip()
        if choice == "1": render_category_view(stats, ROLE_SALUDO)
        elif choice == "2": render_category_view(stats, ROLE_PITCH)
        elif choice == "3": render_category_view(stats, ROLE_CTA)
        elif choice == "4": render_category_view(stats, ROLE_FOLLOW_UP)
        elif choice == "5": render_funnel_view(stats)
        elif choice == "6": export_stats_csv(stats)
        elif choice == "7": break
        else: warn("Opción inválida.")

def render_category_view(stats: Dict[str, Any], role: str):
    while True:
        data = stats[role]
        banner()
        print(style_text(ROLE_LABELS[role], color=Fore.CYAN, bold=True))
        print(full_line(color=Fore.BLUE))
        print(f"Mensajes distintos:        {len(data['messages'])}")
        print(f"Total enviados:          {data['total_sent']:,}")
        print(f"Total respondidos:         {data['total_responded']:,}")
        print(f"{METRIC_NAMES[role]}:              {data['resp_rate']:.1f}%")
        print()
        print(f"Tiempo promedio respuesta: {format_duration(data['avg_time'])}")
        print()
        print("Distribución por horario:")
        for h, val in data["hourly_dist"].items():
            print(f"{h} → {val:.0f}%")

        print(full_line(color=Fore.BLUE))
        print("1) Ver detalle por mensaje")
        print("2) Volver")

        choice = ask("Opción: ").strip()
        if choice == "1": render_message_list(stats, role)
        elif choice == "2": break
        else: warn("Opción inválida.")

def render_message_list(stats: Dict[str, Any], role: str):
    while True:
        data = stats[role]
        banner()
        print(style_text(f"DETALLE DE {ROLE_LABELS[role]}", color=Fore.CYAN, bold=True))
        print(full_line(color=Fore.BLUE))

        for idx, msg in enumerate(data["messages"], 1):
            print(f"[{idx}]")
            print(f"Texto:\n\"{msg['text'][:100]}{'...' if len(msg['text']) > 100 else ''}\"")
            print(f"Enviados: {msg['sent']}")
            print(f"Respondidos: {msg['responded']}")
            print(f"{METRIC_NAMES[role]}: {msg['resp_rate']:.1f}%")
            print(f"Velocidad promedio: {format_duration(msg['avg_time'])}")
            print("-" * 20)

        print("Opciones:")
        print("1) Ver mensaje individual")
        print("2) Volver")

        choice = ask("Opción: ").strip()
        if choice == "1":
            midx = ask_int("Número de mensaje: ", 1, len(data["messages"]))
            if midx:
                render_individual_message(data["messages"][midx-1], role)
        elif choice == "2":
            break
        else: warn("Opción inválida.")

def render_individual_message(msg: Dict[str, Any], role: str):
    while True:
        banner()
        print(style_text("MENSAJE INDIVIDUAL", color=Fore.CYAN, bold=True))
        print(full_line(color=Fore.BLUE))
        print("Texto:")
        print(f"\"{msg['text']}\"")
        print()
        print(f"Enviados: {msg['sent']}")
        print(f"Respondidos: {msg['responded']}")
        print(f"{METRIC_NAMES[role]}: {msg['resp_rate']:.1f}%")
        print()
        print("Tiempo respuesta:")
        print(f"- Promedio: {format_duration(msg['avg_time'])}")
        print(f"- Mínimo: {format_duration(msg['min_time'])}")
        print(f"- Máximo: {format_duration(msg['max_time'])}")
        print()
        print("Respuestas por horario:")
        for h, val in msg["hourly_dist"].items():
            print(f"{h} → {val:.0f}%")

        print(full_line(color=Fore.BLUE))
        print("1) Volver")

        choice = ask("Opción: ").strip()
        if choice == "1": break
        else: warn("Opción inválida.")

def render_funnel_view(stats: Dict[str, Any]):
    banner()
    print(style_text("FUNNEL GENERAL", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))

    for role in [ROLE_SALUDO, ROLE_PITCH, ROLE_CTA, ROLE_FOLLOW_UP]:
        data = stats[role]
        print(f"{ROLE_LABELS[role].capitalize()}:")
        print(f"  Enviados: {data['total_sent']:,}")
        print(f"  {METRIC_NAMES[role]}: {data['resp_rate']:.1f}%")
        print()

    press_enter()

def export_stats_csv(stats: Dict[str, Any]):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = EXPORTS_DIR / filename

    try:
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "message_role", "message_text", "sent_count", "response_count",
                "response_percentage", "avg_response_time_sec", "min_response_time_sec",
                "max_response_time_sec", "hourly_performance"
            ])

            for role in [ROLE_SALUDO, ROLE_PITCH, ROLE_CTA, ROLE_FOLLOW_UP]:
                for msg in stats[role]["messages"]:
                    writer.writerow([
                        role,
                        msg["text"],
                        msg["sent"],
                        msg["responded"],
                        f"{msg['resp_rate']:.1f}%",
                        msg["avg_time"],
                        msg["min_time"],
                        msg["max_time"],
                        json.dumps(msg["hourly_dist"])
                    ])

        print(style_text(f"Datos exportados correctamente a: {filepath}", color=Fore.GREEN))
    except Exception as e:
        warn(f"Error al exportar CSV: {e}")

    press_enter()

def run():
    print("Cargando motor de estadísticas...")
    data = load_conversation_engine()
    if not data or not data.get("conversations"):
        warn("No hay datos de conversaciones para analizar.")
        press_enter()
        return

    categorized = categorize_messages(data)
    if not categorized:
        warn("No se encontraron mensajes enviados en las conversaciones.")
        press_enter()
        return

    stats = calculate_metrics(categorized)
    render_main_menu(stats)

if __name__ == "__main__":
    run()
