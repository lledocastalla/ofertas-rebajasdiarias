#!/usr/bin/env python3
"""
generate_extended_catalog_cron.py — genera catalog_extended.json en su PROPIO proceso, con su
propio cron (26 ago 2026, bug real: esto vivía dentro de update_offers.py y la Pi se quedaba
sin memoria descargando los 7 feeds completos de Leroy Merlin -- el kernel mataba el proceso
con SIGKILL a mitad, lo que también se llevaba por delante el commit/push de offers.json que
venía después en el MISMO proceso, aunque ese código tuviera su propio try/except -- un
SIGKILL no da opción a capturar nada). Aislado aquí: si este proceso muere, el ciclo normal de
update_offers.py (cada 3h) ni se entera.

Throttle propio (EXTENDED_CATALOG_MIN_HOURS, ver update_offers.py) para no intentarlo en cada
disparo del cron -- descargar los 7 feeds completos tarda minutos y es lo que se queda sin
memoria si se hace demasiado a menudo o al mismo tiempo que otra cosa pesada.

Uso: cron aparte, una vez al día en hora tranquila (ver crontab en la Pi).
"""

import fcntl
import json
import subprocess

import update_offers as uo
from multitienda_feeds import generate_extended_catalog


def log(msg):
    print(f"[generate_extended_catalog] {msg}", flush=True)


def main():
    if not uo._should_regenerate_extended_catalog():
        return  # nada que hacer, ya se generó hace menos de EXTENDED_CATALOG_MIN_HOURS

    # Mismo candado que update_offers.py/check_submissions.py -- si el ciclo normal de scraping
    # está corriendo ahora mismo, mejor esperar (bloqueante: esto ya va a tardar minutos de por
    # sí, no hay prisa por competir con el ciclo normal por recursos de la Pi).
    lock_file = open(uo.REPO_LOCK_PATH, "w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)

    try:
        log("Generando catálogo ampliado (Leroy Merlin + Perfumería Comas, para el "
            "buscador)...")
        extended = generate_extended_catalog(log)
        if len(extended) < uo.EXTENDED_CATALOG_MIN_ITEMS:
            log(f"Descartado esta vez: solo {len(extended)} productos (por debajo del "
                f"mínimo de seguridad de {uo.EXTENDED_CATALOG_MIN_ITEMS}).")
            return

        now_iso = uo.datetime.now().astimezone().isoformat(timespec="seconds")
        extended_output = {
            "updated_at": now_iso,
            "affiliate_tag": uo.AFFILIATE_TAG,
            "offers": list(extended.values()),
        }
        with open(uo.EXTENDED_CATALOG_PATH, "w", encoding="utf-8") as f:
            json.dump(extended_output, f, ensure_ascii=False, indent=2)
        log(f"Catálogo ampliado generado: {len(extended)} productos.")

        subprocess.run(["git", "-C", uo.REPO_DIR, "pull", "--quiet"], check=False)
        subprocess.run(["git", "-C", uo.REPO_DIR, "add", "catalog_extended.json"], check=True)
        diff = subprocess.run(["git", "-C", uo.REPO_DIR, "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            log("Sin cambios respecto al commit anterior, no se hace commit.")
        else:
            commit_msg = f"Catálogo ampliado actualizado — {now_iso}"
            subprocess.run(["git", "-C", uo.REPO_DIR, "commit", "-m", commit_msg], check=True)
            push = subprocess.run(["git", "-C", uo.REPO_DIR, "push"], capture_output=True, text=True)
            if push.returncode != 0:
                log(f"ERROR haciendo push: {push.stderr}")
                return
            log("Push realizado correctamente.")

        # Solo se marca como "generado" tras un intento que ha llegado hasta aquí sin
        # excepciones -- si el proceso muriera por SIGKILL antes de esta línea, el próximo
        # disparo del cron simplemente lo vuelve a intentar (el archivo de estado no se habrá
        # actualizado).
        uo._mark_extended_catalog_generated(now_iso)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    main()
