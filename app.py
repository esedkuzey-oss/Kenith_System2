import os
import secrets
import sqlite3
import threading
import requests
import discord

from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from discord.ext import commands

app = Flask(__name__)

DB_NAME = "licenses.db"

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


# ==============================
# DATABASE
# ==============================

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            key TEXT PRIMARY KEY,
            hwid TEXT DEFAULT '',
            discord_user TEXT DEFAULT '',
            days INTEGER NOT NULL,
            expires_at TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ==============================
# DISCORD
# ==============================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    case_insensitive=True
)


@bot.event
async def on_ready():
    print(f"Bot aktif: {bot.user}")


# ==============================
# KEY GENERATOR
# ==============================

def generate_custom_key():
    return (
        "K"
        + "".join(secrets.choice("0123456789") for _ in range(3))
        + "-"
        + "".join(secrets.choice("0123456789") for _ in range(4))
        + "-"
        + "".join(secrets.choice("0123456789") for _ in range(4))
    )


# ==============================
# LICENSE COMMAND
# ==============================

@bot.command(name="license")
@commands.has_permissions(administrator=True)
async def create_license(ctx, arg1: str, member: discord.Member = None):
    
    # İptal / Sıfırlama İşlemi: !license iptal @Kullanici
    if arg1.lower() == "iptal":
        if not member:
            await ctx.send("Kullanım: `!license iptal @Kullanici`")
            return

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE licenses SET hwid = '', expires_at = '' WHERE discord_user = ?", (str(member.id),))
        conn.commit()
        conn.close()
        
        await ctx.send(f"{member.mention} kullanıcısının lisans HWID kilidi sıfırlandı.")
        return

    # Gün Kontrolü (Sadece 7 veya 30 gün)
    try:
        days = int(arg1)
    except ValueError:
        await ctx.send("Geçersiz komut! Sadece **7** veya **30** kullanabilirsin. Örnek: `!license 30 @Kullanici` veya `!license iptal @Kullanici`")
        return

    if days not in (7, 30):
        await ctx.send("Hata: Sadece **7 günlük** veya **30 günlük** lisans oluşturulabilir!")
        return

    if not member:
        await ctx.send("Lütfen bir Discord kullanıcısı etiketleyin. Örnek: `!license 30 @Kullanici`")
        return

    conn = get_db()
    cursor = conn.cursor()
    new_key = None

    try:
        for _ in range(20):
            candidate = generate_custom_key()

            try:
                cursor.execute(
                    """
                    INSERT INTO licenses
                    (key, hwid, discord_user, days, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (candidate, "", str(member.id), days, "")
                )
                new_key = candidate
                break

            except sqlite3.IntegrityError:
                continue

        if new_key is None:
            await ctx.send("Key oluşturulamadı.")
            return

        conn.commit()

        await ctx.send(
            f"Yeni {days} günlük lisans {member.mention} için oluşturuldu:\n"
            f"`{new_key}`"
        )

    except sqlite3.Error as e:
        conn.rollback()
        print(f"Database hatası: {e}")
        await ctx.send("Veritabanı hatası!")

    finally:
        conn.close()


@create_license.error
async def license_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Bu komutu sadece yöneticiler kullanabilir.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Kullanım: `!license 30 @Kullanici` veya `!license iptal @Kullanici`")
    else:
        print(f"License hatası: {error}")
        await ctx.send("Komut hatası!")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Discord hatası: {error}")


# ==============================
# VALIDATION API
# ==============================

@app.route("/validate", methods=["POST"])
def validate():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"status": "ERROR", "message": "Geçersiz JSON!"}), 400

    key = data.get("license_key")
    hwid = data.get("hwid")

    if not isinstance(key, str) or not isinstance(hwid, str):
        return jsonify({"status": "ERROR", "message": "Eksik veri!"}), 400

    key = key.strip()
    hwid = hwid.strip()

    if not key or not hwid or len(hwid) > 256:
        return jsonify({"status": "ERROR", "message": "Eksik veri!"}), 400

    conn = get_db()

    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            """
            SELECT hwid, days, expires_at
            FROM licenses
            WHERE key = ?
            """,
            (key,)
        )

        row = cursor.fetchone()

        if not row:
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Geçersiz anahtar!"}), 403

        saved_hwid, days, expires_at = row
        current_time = datetime.utcnow()

        # İLK AKTİVASYON VE HWID KİLİTLEME
        if not saved_hwid:
            expire_date = current_time + timedelta(days=days)
            formatted_expire = expire_date.strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute(
                """
                UPDATE licenses
                SET hwid = ?, expires_at = ?
                WHERE key = ? AND hwid = ''
                """,
                (hwid, formatted_expire, key)
            )

            conn.commit()

            remaining = expire_date - current_time
            rem_days = remaining.days
            rem_hours = remaining.seconds // 3600
            rem_minutes = (remaining.seconds % 3600) // 60

            if WEBHOOK_URL:
                try:
                    requests.post(
                        WEBHOOK_URL,
                        json={
                            "content": (
                                "Yeni lisans aktivasyonu!\n"
                                f"Key: `{key}`\n"
                                f"HWID: `{hwid}`\n"
                                f"Bitiş: `{formatted_expire}`"
                            )
                        },
                        timeout=5
                    )
                except requests.RequestException as e:
                    print(f"Webhook hatası: {e}")

            return jsonify({
                "status": "SUCCESS",
                "message": "Giriş onaylandı! Lisans cihaza sabitlendi.",
                "remaining_time": f"{rem_days} gün {rem_hours} saat {rem_minutes} dakika",
                "license_bound": True
            })

        # HWID UYUŞMAZLIĞI (BAŞKASI KULLANAMAZ)
        if saved_hwid != hwid:
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Bu anahtar başka bir cihaza kilitli!"}), 403

        # SÜRE KONTROLÜ
        try:
            expire_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Geçersiz bitiş tarihi!"}), 500

        if current_time >= expire_dt:
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Lisans süresi dolmuş!"}), 403

        remaining = expire_dt - current_time
        rem_days = remaining.days
        rem_hours = remaining.seconds // 3600
        rem_minutes = (remaining.seconds % 3600) // 60

        conn.commit()

        return jsonify({
            "status": "SUCCESS",
            "message": "Giriş onaylandı!",
            "remaining_time": f"{rem_days} gün {rem_hours} saat {rem_minutes} dakika",
            "license_bound": True
        })

    except sqlite3.Error as e:
        conn.rollback()
        print(f"Database hatası: {e}")
        return jsonify({"status": "ERROR", "message": "Sunucu veritabanı hatası!"}), 500

    finally:
        conn.close()


# ==============================
# HOME
# ==============================

@app.route("/")
def home():
    return "Kenith Licensing API is running!"


# ==============================
# START BOT
# ==============================

def run_discord_bot():
    if not DISCORD_TOKEN:
        print("HATA: DISCORD_BOT_TOKEN bulunamadı!")
        return

    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"Bot başlatma hatası: {e}")


# ==============================
# START SERVER
# ==============================

if __name__ == "__main__":
    bot_thread = threading.Thread(
        target=run_discord_bot,
        daemon=True
    )
    bot_thread.start()

    port = int(os.environ.get("PORT", 5000))
    print("Kenith Licensing API başlatılıyor...")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )