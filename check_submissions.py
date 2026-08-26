#!/usr/bin/env python3
"""
check_submissions.py — comprobación RÁPIDA de "Sugerir una oferta" (26 ago 2026, pedido
explícito del usuario: "debería subir el anuncio al instante si es correcto ya que estas
ofertas duran poco o nada").

Cron APARTE, cada pocos minutos -- SEPARADO del ciclo normal de update_offers.py (cada 3h,
sondea Amazon por keyword). Si no hay ninguna sugerencia pendiente en Firestore, termina al
momento sin abrir un navegador (una consulta a Firestore cada pocos minutos no gasta ni de
lejos la cuota diaria gratis). Reutiliza literalmente las mismas funciones que
update_offers.py (import directo del propio script, sin duplicar lógica) -- update_offers.py
YA NO procesa sugerencias en su ciclo normal, esa responsabilidad vive aquí en exclusiva desde
hoy, para que los dos procesos nunca revisen la misma sugerencia a la vez.

Uso: cron cada 3-5 min, ver crontab en RASPI_REBAJASDIARIAS.md.
"""

import fcntl
import json
import os
import subprocess
from datetime import datetime

import update_offers as uo


def log(msg):
    print(f"[check_submissions] {msg}", flush=True)


def main():
    if not os.path.isdir(uo.REPO_DIR):
        log(f"ERROR: no existe {uo.REPO_DIR}, aborto")
        return

    # Candado compartido con update_offers.py (mismo archivo, ver REPO_LOCK_PATH) -- si el
    # ciclo normal de 3h está corriendo ahora mismo, este script se sale sin más y lo reintenta
    # en su próximo disparo (cada pocos minutos, no pasa nada por saltarse una vez). NO
    # bloqueante a propósito: la gracia de este script es ser rápido, no tiene sentido
    # esperarse minutos a que termine un ciclo de scraping entero.
    lock_file = open(uo.REPO_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("el repo está en uso (update_offers.py u otra instancia de este script) ahora "
            "mismo, se sale sin hacer nada -- se reintenta en el próximo disparo del cron")
        return

    # Primero, sin tocar git ni abrir un navegador: ¿hay algo que hacer? La inmensa mayoría de
    # los disparos de este cron no encontrarán nada pendiente -- salir aquí es lo normal, no la
    # excepción. tracked_offers no vacío no significa que haya un borrado real que reconciliar
    # (normalmente no lo hay), pero comprobarlo es barato (solo lecturas de Firestore, sin
    # navegador) así que se deja para más abajo sin más filtro que este.
    pending = uo._fetch_pending_submissions(log)
    tracked_offers = uo._load_submission_offers()
    if not pending and not tracked_offers:
        return

    subprocess.run(["git", "-C", uo.REPO_DIR, "pull", "--quiet"], check=False)

    try:
        with open(uo.OFFERS_PATH, encoding="utf-8") as f:
            existing = json.load(f)
    except Exception as e:
        log(f"ERROR leyendo offers.json existente: {e}. Aborto por seguridad.")
        return
    existing_offers = {o["id"]: o for o in existing.get("offers", []) if o.get("id")}

    approved = {}
    if pending:
        log(f"{len(pending)} sugerencia(s) pendiente(s), comprobando ahora...")
        driver = None
        try:
            driver = uo.build_driver()
            approved = uo._process_submissions(driver, log)
        except Exception as e:
            log(f"ERROR comprobando sugerencias: {e}")
        finally:
            if driver is not None:
                driver.quit()

    removed = set()
    try:
        removed = uo._reconcile_deleted_submissions(log)
    except Exception as e:
        log(f"aviso: fallo comprobando sugerencias borradas: {e}")

    if not approved and not removed:
        log("nada que publicar ni retirar esta vez.")
        return

    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    merged = dict(existing_offers)
    for offer_id in removed:
        merged.pop(offer_id, None)
    for asin, new_offer in approved.items():
        prior = existing_offers.get(asin)
        new_offer["first_seen"] = prior["first_seen"] if prior else now_iso
        new_offer["last_seen"] = now_iso
        merged[asin] = new_offer

    output = {
        "updated_at": now_iso,
        "affiliate_tag": uo.AFFILIATE_TAG,
        "offers": uo.diversify_order(list(merged.values())),
    }
    with open(uo.OFFERS_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    git_paths = ["offers.json"]
    if os.path.isfile(uo.SUBMISSION_OFFERS_PATH):
        git_paths.append("submission_offers.json")
    subprocess.run(["git", "-C", uo.REPO_DIR, "add"] + git_paths, check=True)
    diff = subprocess.run(["git", "-C", uo.REPO_DIR, "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        log("sin cambios reales respecto al commit anterior, no se hace commit.")
        return

    commit_msg = (
        f"Sugerencias de usuarios: {len(approved)} publicada(s), {len(removed)} "
        f"retirada(s) — {now_iso}"
    )
    subprocess.run(["git", "-C", uo.REPO_DIR, "commit", "-m", commit_msg], check=True)
    push = subprocess.run(["git", "-C", uo.REPO_DIR, "push"], capture_output=True, text=True)
    if push.returncode != 0:
        log(f"ERROR haciendo push: {push.stderr}")
        return
    log("push realizado correctamente.")

    if approved:
        uo.notify_telegram(
            f"⚡ Oferta sugerida por un usuario publicada al instante: {len(approved)} nueva(s)."
        )
        uo.notify_app_push(list(merged.values()))


if __name__ == "__main__":
    main()
