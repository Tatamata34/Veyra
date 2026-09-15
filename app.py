import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote
import json
import threading
import time
import secrets
import hashlib
import base64
import csv
import io
import requests

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, flash, session, has_app_context, abort, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import inspect

load_dotenv()

app = Flask(__name__, template_folder="app/templates", static_folder="app/static")
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///nox_store.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
UPLOAD_DIR = os.path.join(app.static_folder, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

db = SQLAlchemy(app)

def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token

@app.context_processor
def inject_security():
    return {"csrf_token": csrf_token()}

@app.before_request
def security_guard():
    session.permanent = True
    if request.method == "POST":
        expected = session.get("csrf_token")
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            abort(400, description="Invalid CSRF token")

login_manager = LoginManager(app)
login_manager.login_view = "login"

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    telegram_id = db.Column(db.String(64), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text, default="")
    image_url = db.Column(db.String(500), default="")
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False)
    active = db.Column(db.Boolean, default=True)
    featured = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    card_label = db.Column(db.String(140), nullable=False, default="")
    price_label = db.Column(db.String(60), nullable=False, default="month")
    category = db.relationship("Category", backref="products")
    plans = db.relationship("Plan", backref="product", cascade="all, delete-orphan")

class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    months = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    sale_price = db.Column(db.Float, nullable=True)
    cost_price = db.Column(db.Float, nullable=False, default=0.0)
    active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    discount_percent = db.Column(db.Float, default=0.0)
    discount_fixed = db.Column(db.Float, default=0.0)
    max_uses = db.Column(db.Integer, default=0)
    used_count = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.DateTime, nullable=True)
    active = db.Column(db.Boolean, default=True)

class CouponUsage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    coupon_id = db.Column(db.Integer, db.ForeignKey("coupon.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=True)
    used_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("coupon_id", "user_id", name="uq_coupon_user"),)

class Wishlist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "product_id", name="uq_wishlist_user_product"),)
    product = db.relationship("Product")

class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, default="")
    approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")
    product = db.relationship("Product")

class Referral(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    referred_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    reward_points = db.Column(db.Integer, default=0)
    rewarded = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class LoyaltyPoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    points = db.Column(db.Integer, default=0)
    reason = db.Column(db.String(160), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Bundle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    price = db.Column(db.Float, nullable=False)
    active = db.Column(db.Boolean, default=True)
    featured = db.Column(db.Boolean, default=False)
    items = db.relationship("BundleItem", backref="bundle", cascade="all, delete-orphan")

class BundleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bundle_id = db.Column(db.Integer, db.ForeignKey("bundle.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    plan = db.relationship("Plan")

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=True)
    bundle_id = db.Column(db.Integer, db.ForeignKey("bundle.id"), nullable=True)
    status = db.Column(db.String(30), default="pending")
    sale_price = db.Column(db.Float, nullable=False, default=0.0)
    cost_price = db.Column(db.Float, nullable=False, default=0.0)
    profit = db.Column(db.Float, nullable=False, default=0.0)
    discount = db.Column(db.Float, nullable=False, default=0.0)
    coupon_code = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")
    plan = db.relationship("Plan")
    bundle = db.relationship("Bundle")

class OrderDelivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), unique=True, nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    delivered_at = db.Column(db.DateTime, default=datetime.utcnow)
    delivered_by_telegram_id = db.Column(db.String(64), nullable=True)
    sent_to_customer_telegram = db.Column(db.Boolean, default=False)
    order = db.relationship("Order", backref=db.backref("delivery", uselist=False))

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    active = db.Column(db.Boolean, default=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=True)
    user = db.relationship("User")
    product = db.relationship("Product")

class AdminSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text, default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TelegramLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    token = db.Column(db.String(80), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")

class TelegramProfile(db.Model):
    """Telegram identity captured when a customer contacts/links the bot.
    Kept in its own table so this feature can be added to an existing DB
    without requiring ALTER TABLE on the User table.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    telegram_id = db.Column(db.String(64), unique=True, nullable=False)
    username = db.Column(db.String(120), nullable=True)
    first_name = db.Column(db.String(120), nullable=True)
    last_name = db.Column(db.String(120), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship("User")

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(120), nullable=False)
    details = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    admin_user = db.relationship("User")

class V21CatalogBackup(db.Model):
    __tablename__ = "v21_catalog_backups"
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(180), nullable=False)
    kind = db.Column(db.String(30), default="snapshot")
    payload = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    admin_user = db.relationship("User")

TRANSLATIONS = {
    "en": {
        "Home":"Home", "Products":"Products", "Categories":"Categories", "Deals":"Deals", "Support":"Support", "Login":"Login", "Create Account":"Create Account", "Account":"Account", "Admin":"Admin", "Logout":"Logout",
        "All Products":"All Products", "View Deals":"View Deals", "Browse Products →":"Browse Products →", "Instant Delivery":"Instant Delivery", "Secure & Safe":"Secure & Safe", "24/7 Support":"24/7 Support", "Trusted Store":"Trusted Store", "Special Offers":"Special Offers", "Bundle Offers":"Bundle Offers", "Featured Products":"Featured Products", "View Product":"View Product", "Get Bundle →":"Get Bundle →", "View All Deals →":"View All Deals →", "View All Products →":"View All Products →", "Choose your plan":"Choose your plan", "Order":"Order", "Open WhatsApp":"Open WhatsApp", "Customer reviews":"Customer reviews", "Submit Review":"Submit Review", "Active subscriptions":"Active subscriptions", "Orders":"Orders", "No subscriptions yet.":"No subscriptions yet.", "No orders yet.":"No orders yet.", "Business Dashboard":"Business Dashboard", "Total sales":"Total sales", "Total cost":"Total cost", "Net product profit":"Net product profit", "Orders & profit":"Orders & profit", "Products & pricing":"Products & pricing", "Save":"Save", "Customers":"Customers", "Coupons":"Coupons", "Reviews waiting for approval":"Reviews waiting for approval", "Approve":"Approve", "Reject":"Reject", "Store activity":"Store activity", "Wishlist items":"Wishlist items", "Loyalty points issued":"Loyalty points issued", "Register":"Register", "Password":"Password", "Username":"Username", "Email":"Email", "Welcome":"Welcome", "Referral":"Referral", "Rewards":"Rewards", "My referral link":"My referral link", "Referral points":"Referral points", "Earn points only after your referred friend makes a purchase.":"Earn points only after your referred friend makes a purchase.",
    },
    "de": {
        "Home":"Startseite", "Products":"Produkte", "Categories":"Kategorien", "Deals":"Angebote", "Support":"Support", "Login":"Anmelden", "Create Account":"Konto erstellen", "Account":"Konto", "Admin":"Admin", "Logout":"Abmelden", "All Products":"Alle Produkte", "View Deals":"Angebote ansehen", "Browse Products →":"Produkte ansehen →", "Instant Delivery":"Sofortige Lieferung", "Secure & Safe":"Sicher & geschützt", "24/7 Support":"24/7 Support", "Trusted Store":"Vertrauenswürdiger Shop", "Special Offers":"Sonderangebote", "Bundle Offers":"Bundle-Angebote", "Featured Products":"Empfohlene Produkte", "View Product":"Produkt ansehen", "Get Bundle →":"Bundle kaufen →", "View All Deals →":"Alle Angebote →", "View All Products →":"Alle Produkte →", "Choose your plan":"Tarif wählen", "Order":"Bestellen", "Open WhatsApp":"WhatsApp öffnen", "Customer reviews":"Kundenbewertungen", "Submit Review":"Bewertung senden", "Active subscriptions":"Aktive Abonnements", "Orders":"Bestellungen", "No subscriptions yet.":"Noch keine Abonnements.", "No orders yet.":"Noch keine Bestellungen.", "Business Dashboard":"Business-Dashboard", "Total sales":"Gesamtumsatz", "Total cost":"Gesamtkosten", "Net product profit":"Nettogewinn", "Orders & profit":"Bestellungen & Gewinn", "Products & pricing":"Produkte & Preise", "Save":"Speichern", "Customers":"Kunden", "Coupons":"Gutscheine", "Reviews waiting for approval":"Bewertungen zur Freigabe", "Approve":"Freigeben", "Reject":"Ablehnen", "Store activity":"Shop-Aktivität", "Wishlist items":"Wunschlisten-Einträge", "Loyalty points issued":"Vergebene Treuepunkte", "Register":"Registrieren", "Password":"Passwort", "Username":"Benutzername", "Email":"E-Mail", "Welcome":"Willkommen", "Referral":"Empfehlungen", "Rewards":"Prämien", "My referral link":"Mein Empfehlungslink", "Referral points":"Empfehlungspunkte", "Earn points only after your referred friend makes a purchase.":"Punkte gibt es erst, wenn dein geworbener Freund einen Kauf tätigt.",
    },
    "fr": {
        "Home":"Accueil", "Products":"Produits", "Categories":"Catégories", "Deals":"Offres", "Support":"Assistance", "Login":"Connexion", "Create Account":"Créer un compte", "Account":"Compte", "Admin":"Admin", "Logout":"Déconnexion", "All Products":"Tous les produits", "View Deals":"Voir les offres", "Browse Products →":"Voir les produits →", "Instant Delivery":"Livraison instantanée", "Secure & Safe":"Sûr et sécurisé", "24/7 Support":"Assistance 24/7", "Trusted Store":"Boutique de confiance", "Special Offers":"Offres spéciales", "Bundle Offers":"Offres groupées", "Featured Products":"Produits populaires", "View Product":"Voir le produit", "Get Bundle →":"Acheter le bundle →", "View All Deals →":"Voir toutes les offres →", "View All Products →":"Voir tous les produits →", "Choose your plan":"Choisissez votre formule", "Order":"Commander", "Open WhatsApp":"Ouvrir WhatsApp", "Customer reviews":"Avis clients", "Submit Review":"Envoyer l'avis", "Active subscriptions":"Abonnements actifs", "Orders":"Commandes", "No subscriptions yet.":"Aucun abonnement pour le moment.", "No orders yet.":"Aucune commande pour le moment.", "Business Dashboard":"Tableau de bord", "Total sales":"Ventes totales", "Total cost":"Coût total", "Net product profit":"Bénéfice net", "Orders & profit":"Commandes & bénéfice", "Products & pricing":"Produits & tarifs", "Save":"Enregistrer", "Customers":"Clients", "Coupons":"Coupons", "Reviews waiting for approval":"Avis en attente", "Approve":"Approuver", "Reject":"Rejeter", "Store activity":"Activité de la boutique", "Wishlist items":"Articles favoris", "Loyalty points issued":"Points fidélité distribués", "Register":"Inscription", "Password":"Mot de passe", "Username":"Nom d'utilisateur", "Email":"E-mail", "Welcome":"Bienvenue", "Referral":"Parrainage", "Rewards":"Récompenses", "My referral link":"Mon lien de parrainage", "Referral points":"Points de parrainage", "Earn points only after your referred friend makes a purchase.":"Les points sont attribués uniquement après l'achat de votre filleul.",
    },
    "sq": {
        "Home":"Ballina", "Products":"Produktet", "Categories":"Kategoritë", "Deals":"Oferta", "Support":"Mbështetje", "Login":"Hyr", "Create Account":"Krijo llogari", "Account":"Llogaria", "Admin":"Admin", "Logout":"Dil", "All Products":"Të gjitha produktet", "View Deals":"Shiko ofertat", "Browse Products →":"Shiko produktet →", "Instant Delivery":"Dorëzim i menjëhershëm", "Secure & Safe":"Sigurt & i mbrojtur", "24/7 Support":"Mbështetje 24/7", "Trusted Store":"Dyqan i besueshëm", "Special Offers":"Oferta speciale", "Bundle Offers":"Oferta Bundle", "Featured Products":"Produktet e zgjedhura", "View Product":"Shiko produktin", "Get Bundle →":"Bli Bundle →", "View All Deals →":"Shiko të gjitha ofertat →", "View All Products →":"Shiko të gjitha produktet →", "Choose your plan":"Zgjidh planin", "Order":"Porosit", "Open WhatsApp":"Hap WhatsApp", "Customer reviews":"Vlerësimet e klientëve", "Submit Review":"Dërgo vlerësimin", "Active subscriptions":"Abonimet aktive", "Orders":"Porositë", "No subscriptions yet.":"Ende nuk ka abonime.", "No orders yet.":"Ende nuk ka porosi.", "Business Dashboard":"Paneli i biznesit", "Total sales":"Shitjet totale", "Total cost":"Kostoja totale", "Net product profit":"Fitimi neto", "Orders & profit":"Porositë & fitimi", "Products & pricing":"Produktet & çmimet", "Save":"Ruaj", "Customers":"Klientët", "Coupons":"Kuponët", "Reviews waiting for approval":"Vlerësime në pritje", "Approve":"Aprovo", "Reject":"Refuzo", "Store activity":"Aktiviteti i dyqanit", "Wishlist items":"Artikujt në dëshira", "Loyalty points issued":"Pikët e besnikërisë", "Register":"Regjistrohu", "Password":"Fjalëkalimi", "Username":"Username", "Email":"Email", "Welcome":"Mirë se erdhe", "Referral":"Referimi", "Rewards":"Shpërblimet", "My referral link":"Linku im i referimit", "Referral points":"Pikë referimi", "Earn points only after your referred friend makes a purchase.":"Pikët fitohen vetëm pasi personi i referuar bën një blerje.",
    }
}


# Phase 6 analytics / UX vocabulary.
for _lang, _vals in {
    "en": {"Analytics":"Analytics","days":"days","Average order":"Average order","Net profit margin":"Net profit margin","Points issued":"Points issued","Sales — last 14 days":"Sales — last 14 days","Non-cancelled orders":"Non-cancelled orders","Top products":"Top products","By recorded sales":"By recorded sales","No sales yet.":"No sales yet.","More":"More","Explore":"Explore","View products":"View products","Shop bundles":"Shop bundles","Special offer":"Special offer","Recommended":"Recommended","Better prices. Simple choice.":"Better prices. Simple choice.","See the products customers choose most.":"See the products customers choose most.","Find your next favorite.":"Find your next favorite.","Popular digital products in one place.":"Popular digital products in one place.","Clean":"Clean","Glass":"Glass","Dark":"Dark","Neon":"Neon"},
    "de": {"Analytics":"Analysen","days":"Tage","Average order":"Durchschnittliche Bestellung","Net profit margin":"Nettogewinnmarge","Points issued":"Vergebene Punkte","Sales — last 14 days":"Umsatz — letzte 14 Tage","Non-cancelled orders":"Nicht stornierte Bestellungen","Top products":"Top-Produkte","By recorded sales":"Nach erfasstem Umsatz","No sales yet.":"Noch keine Verkäufe.","More":"Mehr","Explore":"Entdecken","View products":"Produkte ansehen","Shop bundles":"Bundles ansehen","Special offer":"Sonderangebot","Recommended":"Empfohlen","Better prices. Simple choice.":"Bessere Preise. Einfache Auswahl.","See the products customers choose most.":"Sieh dir die beliebtesten Produkte an.","Find your next favorite.":"Finde deinen nächsten Favoriten.","Popular digital products in one place.":"Beliebte digitale Produkte an einem Ort.","Clean":"Clean","Glass":"Glass","Dark":"Dunkel","Neon":"Neon"},
    "fr": {"Analytics":"Analyses","days":"jours","Average order":"Commande moyenne","Net profit margin":"Marge nette","Points issued":"Points distribués","Sales — last 14 days":"Ventes — 14 derniers jours","Non-cancelled orders":"Commandes non annulées","Top products":"Meilleurs produits","By recorded sales":"Par ventes enregistrées","No sales yet.":"Aucune vente pour le moment.","More":"Plus","Explore":"Explorer","View products":"Voir les produits","Shop bundles":"Voir les bundles","Special offer":"Offre spéciale","Recommended":"Recommandé","Better prices. Simple choice.":"De meilleurs prix. Un choix simple.","See the products customers choose most.":"Découvrez les produits les plus choisis.","Find your next favorite.":"Trouvez votre prochain favori.","Popular digital products in one place.":"Les produits numériques populaires au même endroit.","Clean":"Clair","Glass":"Verre","Dark":"Sombre","Neon":"Néon"},
    "sq": {"Analytics":"Analitika","days":"ditë","Average order":"Porosia mesatare","Net profit margin":"Marzhi i fitimit neto","Points issued":"Pikë të dhëna","Sales — last 14 days":"Shitjet — 14 ditët e fundit","Non-cancelled orders":"Porosi jo të anuluara","Top products":"Produktet kryesore","By recorded sales":"Sipas shitjeve të regjistruara","No sales yet.":"Ende nuk ka shitje.","More":"Më shumë","Explore":"Eksploro","View products":"Shiko produktet","Shop bundles":"Shiko Bundles","Special offer":"Ofertë speciale","Recommended":"Rekomanduar","Better prices. Simple choice.":"Çmime më të mira. Zgjedhje e thjeshtë.","See the products customers choose most.":"Shiko produktet që zgjedhin më shumë klientët.","Find your next favorite.":"Gjej të preferuarin tënd të radhës.","Popular digital products in one place.":"Produktet digjitale të njohura në një vend.","Clean":"E pastër","Glass":"Glass","Dark":"E errët","Neon":"Neon"}
}.items():
    TRANSLATIONS[_lang].update(_vals)

TRANSLATIONS["en"].update({"Wishlist":"Wishlist","Saved products":"Saved products","View Product":"View Product","Your wishlist is empty.":"Your wishlist is empty.","Loyalty points":"Loyalty points","Delivery":"Delivery"})
TRANSLATIONS["de"].update({"Wishlist":"Wunschliste","Saved products":"Gespeicherte Produkte","Your wishlist is empty.":"Deine Wunschliste ist leer.","Loyalty points":"Treuepunkte","Delivery":"Lieferung"})
TRANSLATIONS["fr"].update({"Wishlist":"Liste de souhaits","Saved products":"Produits enregistrés","Your wishlist is empty.":"Votre liste de souhaits est vide.","Loyalty points":"Points fidélité","Delivery":"Livraison"})
TRANSLATIONS["sq"].update({"Wishlist":"Lista e dëshirave","Saved products":"Produktet e ruajtura","Your wishlist is empty.":"Lista e dëshirave është bosh.","Loyalty points":"Pikë besnikërie","Delivery":"Dorëzimi"})

TRANSLATIONS["en"].update({"Customer account":"Customer account","Welcome, @":"Welcome, @","Started":"Started","No approved reviews yet.":"No approved reviews yet.","Back to products":"Back to products","Add / Remove Wishlist":"Add / Remove Wishlist","Full access for":"Full access for","Protected admin area":"Protected admin area","Sales, costs and real profit in one place.":"Sales, costs and real profit in one place.","Revenue from orders":"Revenue from orders","Supplier / acquisition cost":"Supplier / acquisition cost","Sales − cost":"Sales − cost","Total recorded orders":"Total recorded orders","Historical orders keep their original cost/sale/profit.":"Historical orders keep their original cost/sale/profit.","Bundle cost is calculated from the selected plans' acquisition costs; the sale price is the bundle price.":"Bundle cost is calculated from the selected plans' acquisition costs; the sale price is the bundle price.","Code":"Code","Discount":"Discount","Uses":"Uses","Expires":"Expires","Order #":"Order #","Status:":"Status:","Create account":"Create account","Pa emër dhe mbiemër.":"No first or last name.","Open WhatsApp":"Open WhatsApp"})

# Complete UI dictionary used by the public store and admin center.
_EXTRA = {
"Digital marketplace":"Digital marketplace","Digital access. Real possibilities.":"Digital access. Real possibilities.","Premium digital products in one simple marketplace.":"Premium digital products in one simple marketplace.","Digital products. Better value.":"Digital products. Better value.","More possibilities.":"More possibilities.","Less spending.":"Less spending.","Get the digital services you use every day at better prices, with simple ordering and real support.":"Get the digital services you use every day at better prices, with simple ordering and real support.","Browse Products":"Browse Products","See Deals":"See Deals","Clear prices":"Clear prices","Easy ordering":"Easy ordering","Real support":"Real support","All":"All","Shop":"Shop","Popular products":"Popular products","Choose what you need. Keep it simple.":"Choose what you need. Keep it simple.","products":"products","product":"product","Search products...":"Search products...","month":"month","months":"months","Popular":"Popular","from":"from","View product":"View product","No products match your search.":"No products match your search.","Save more":"Save more","Bundle deals":"Bundle deals","Put several services together and pay one special price.":"Put several services together and pay one special price.","BUNDLE":"BUNDLE","Special price":"Special price","Get bundle":"Get bundle","Login to order":"Login to order","Simple ordering":"Simple ordering","No unnecessary steps.":"No unnecessary steps.","Transparent pricing":"Transparent pricing","See the price before ordering.":"See the price before ordering.","One account":"One account","Orders and subscriptions together.":"Orders and subscriptions together.","We are here when you need us.":"We are here when you need us.","A smarter way to buy":"A smarter way to buy","Why pay full price when you can pay less?":"Why pay full price when you can pay less?","Great digital services, better value. Simple, transparent and made for everyday use.":"Great digital services, better value. Simple, transparent and made for everyday use.","Explore now":"Explore now","Change theme":"Change theme","Shop":"Shop","Help":"Help","Company":"Company","FAQ":"FAQ","Delivery":"Delivery","About":"About","Privacy":"Privacy","Terms":"Terms","All rights reserved.":"All rights reserved.","Back to products":"Back to products","Add / Remove Wishlist":"Add / Remove Wishlist","Choose your plan":"Choose your plan","Full access for":"Full access for","Coupon":"Coupon","Order":"Order","Customer reviews":"Customer reviews","Write a review...":"Write a review...","Submit Review":"Submit Review","No approved reviews yet.":"No approved reviews yet.","Welcome back":"Welcome back","Sign in to manage your orders and subscriptions.":"Sign in to manage your orders and subscriptions.","No account yet?":"No account yet?","Create your account":"Create your account","Only username, email and password.":"Only username, email and password.","Already have an account?":"Already have an account?","Customer account":"Customer account","Active subscriptions":"Active subscriptions","Expires":"Expires","Active":"Active","No subscriptions yet.":"No subscriptions yet.","Rewards":"Rewards","Loyalty points":"Loyalty points","My referral link":"My referral link","Referral points are earned only after your referred friend completes a purchase.":"Referral points are earned only after your referred friend completes a purchase.","Orders":"Orders","No orders yet.":"No orders yet.","Order created":"Order created","Your order is ready. Contact us on WhatsApp to complete it.":"Your order is ready. Contact us on WhatsApp to complete it.","Go to account":"Go to account","Veyra Control":"Veyra Control","Manage everything from one place":"Manage everything from one place","Dashboard":"Dashboard","Bundles":"Bundles","Marketing":"Marketing","Settings":"Settings","Activity":"Activity","View store":"View store","Control center":"Control center","Good control. Less work.":"Good control. Less work.","Run products, prices, offers, orders and customers without touching code.":"Run products, prices, offers, orders and customers without touching code.","Product":"Product","Bundle":"Bundle","Sales":"Sales","Cost":"Cost","Profit":"Profit","non-cancelled orders":"non-cancelled orders","acquisition cost":"acquisition cost","sales minus cost":"sales minus cost","pending":"pending","registered":"registered","active":"active","Catalog":"Catalog","Add products, images, plans, prices and visibility from here.":"Add products, images, plans, prices and visibility from here.","Product name":"Product name","Category":"Category","URL slug":"URL slug","Short description":"Short description","Image URL":"Image URL","Replace image":"Replace image","Months":"Months","Price":"Price","Sale price":"Sale price","Your cost":"Your cost","Visible":"Visible","Featured":"Featured","Create product":"Create product","Name":"Name","Description":"Description","Slug":"Slug","Live":"Live","Hidden":"Hidden","plans":"plans","Plans & pricing":"Plans & pricing","Public price · sale price · cost":"Public price · sale price · cost","On":"On","Save":"Save","Remove this plan?":"Remove this plan?","New plan":"New plan","Add plan":"Add plan","Remove / Archive product":"Remove / Archive product","Offers":"Offers","Build bundles, choose plans and set one special price.":"Build bundles, choose plans and set one special price.","Bundle name":"Bundle name","Bundle price":"Bundle price","items":"items","Save bundle":"Save bundle","Remove this bundle?":"Remove this bundle?","Remove":"Remove","No bundles yet.":"No bundles yet.","Completed orders activate subscriptions and referral rewards.":"Completed orders activate subscriptions and referral rewards.","Customer":"Customer","Sale":"Sale","Status":"Status","No customers yet.":"No customers yet.","People":"People","Trust":"Trust","Reviews":"Reviews","Approve":"Approve","Reject":"Reject","Approved":"Approved","No reviews yet.":"No reviews yet.","Create discounts without touching code.":"Create discounts without touching code.","Code":"Code","Max uses":"Max uses","Create coupon":"Create coupon","used":"used","Disable":"Disable","Enable":"Enable","Automation":"Automation","Connect two admin Telegram IDs and receive order notifications.":"Connect two admin Telegram IDs and receive order notifications.","Connected":"Connected","Not configured":"Not configured","Admin 1 Telegram ID":"Admin 1 Telegram ID","Admin 2 Telegram ID":"Admin 2 Telegram ID","Save Telegram IDs":"Save Telegram IDs","Send test notification":"Send test notification","Bot commands":"Bot commands","Admin commands":"Admin commands","Store":"Store","Settings & appearance":"Settings & appearance","Control the look, language and public contact details.":"Control the look, language and public contact details.","Store name":"Store name","Support WhatsApp":"Support WhatsApp","Support email":"Support email","Default language":"Default language","Hero title – English":"Hero title – English","Hero title – German":"Hero title – German","Hero title – French":"Hero title – French","Hero title – Albanian":"Hero title – Albanian","Announcement":"Announcement","Optional top announcement":"Optional top announcement","Store logo URL":"Store logo URL","Default theme":"Default theme","Maintenance mode":"Maintenance mode","Save settings":"Save settings","Available themes":"Available themes","Security":"Security","Activity log":"Activity log","See what admins changed.":"See what admins changed.","Clear activity log?":"Clear activity log?","Clear log":"Clear log","No activity yet.":"No activity yet."
}
for _k,_v in _EXTRA.items(): TRANSLATIONS["en"].setdefault(_k,_v)
TRANSLATIONS["de"].update({
"Digital marketplace":"Digitaler Marktplatz","Digital access. Real possibilities.":"Digitaler Zugang. Echte Möglichkeiten.","Premium digital products in one simple marketplace.":"Premium-Digitalprodukte auf einem einfachen Marktplatz.","Digital products. Better value.":"Digitale Produkte. Besserer Wert.","More possibilities.":"Mehr Möglichkeiten.","Less spending.":"Weniger Ausgaben.","Get the digital services you use every day at better prices, with simple ordering and real support.":"Digitale Dienste des Alltags zu besseren Preisen – einfach bestellen und mit echtem Support.","Browse Products":"Produkte ansehen","See Deals":"Angebote ansehen","Clear prices":"Klare Preise","Easy ordering":"Einfach bestellen","Real support":"Echter Support","All":"Alle","Shop":"Shop","Popular products":"Beliebte Produkte","Choose what you need. Keep it simple.":"Wähle, was du brauchst. Einfach halten.","products":"Produkte","product":"Produkt","Search products...":"Produkte suchen...","month":"Monat","months":"Monate","Popular":"Beliebt","from":"ab","View product":"Produkt ansehen","No products match your search.":"Keine Produkte passen zu deiner Suche.","Save more":"Mehr sparen","Bundle deals":"Bundle-Angebote","Put several services together and pay one special price.":"Kombiniere mehrere Dienste und zahle einen Sonderpreis.","Special price":"Sonderpreis","Get bundle":"Bundle kaufen","Login to order":"Zum Bestellen anmelden","Simple ordering":"Einfach bestellen","No unnecessary steps.":"Keine unnötigen Schritte.","Transparent pricing":"Transparente Preise","See the price before ordering.":"Preis vor der Bestellung sehen.","One account":"Ein Konto","Orders and subscriptions together.":"Bestellungen und Abos zusammen.","We are here when you need us.":"Wir sind für dich da.","A smarter way to buy":"Cleverer einkaufen","Why pay full price when you can pay less?":"Warum den vollen Preis zahlen, wenn es günstiger geht?","Great digital services, better value. Simple, transparent and made for everyday use.":"Starke digitale Dienste, besserer Wert. Einfach, transparent und für den Alltag gemacht.","Explore now":"Jetzt entdecken","Change theme":"Theme ändern","Help":"Hilfe","Company":"Unternehmen","FAQ":"FAQ","Delivery":"Lieferung","About":"Über uns","Privacy":"Datenschutz","Terms":"AGB","All rights reserved.":"Alle Rechte vorbehalten.","Back to products":"Zurück zu den Produkten","Add / Remove Wishlist":"Zur Wunschliste hinzufügen / entfernen","Choose your plan":"Tarif wählen","Full access for":"Voller Zugriff für","Coupon":"Gutschein","Order":"Bestellen","Customer reviews":"Kundenbewertungen","Write a review...":"Bewertung schreiben...","Submit Review":"Bewertung senden","No approved reviews yet.":"Noch keine freigegebenen Bewertungen.","Welcome back":"Willkommen zurück","Sign in to manage your orders and subscriptions.":"Melde dich an, um Bestellungen und Abos zu verwalten.","No account yet?":"Noch kein Konto?","Create your account":"Konto erstellen","Only username, email and password.":"Nur Benutzername, E-Mail und Passwort.","Already have an account?":"Schon ein Konto?","Customer account":"Kundenkonto","Active subscriptions":"Aktive Abonnements","Expires":"Läuft ab","Active":"Aktiv","No subscriptions yet.":"Noch keine Abonnements.","Rewards":"Prämien","Loyalty points":"Treuepunkte","My referral link":"Mein Empfehlungslink","Referral points are earned only after your referred friend completes a purchase.":"Empfehlungspunkte gibt es erst nach dem abgeschlossenen Kauf deines Freundes.","Orders":"Bestellungen","No orders yet.":"Noch keine Bestellungen.","Order created":"Bestellung erstellt","Your order is ready. Contact us on WhatsApp to complete it.":"Deine Bestellung ist bereit. Kontaktiere uns über WhatsApp, um sie abzuschließen.","Go to account":"Zum Konto","Veyra Control":"Veyra Control","Manage everything from one place":"Alles an einem Ort verwalten","Dashboard":"Dashboard","Bundles":"Bundles","Marketing":"Marketing","Settings":"Einstellungen","Activity":"Aktivität","View store":"Shop ansehen","Control center":"Kontrollzentrum","Good control. Less work.":"Mehr Kontrolle. Weniger Arbeit.","Run products, prices, offers, orders and customers without touching code.":"Produkte, Preise, Angebote, Bestellungen und Kunden ohne Code verwalten.","Product":"Produkt","Bundle":"Bundle","Sales":"Umsatz","Cost":"Kosten","Profit":"Gewinn","non-cancelled orders":"nicht stornierte Bestellungen","acquisition cost":"Einkaufskosten","sales minus cost":"Umsatz minus Kosten","pending":"offen","registered":"registriert","active":"aktiv","Catalog":"Katalog","Add products, images, plans, prices and visibility from here.":"Produkte, Bilder, Tarife, Preise und Sichtbarkeit hier verwalten.","Product name":"Produktname","Category":"Kategorie","URL slug":"URL-Slug","Short description":"Kurzbeschreibung","Image URL":"Bild-URL","Replace image":"Bild ersetzen","Months":"Monate","Price":"Preis","Sale price":"Angebotspreis","Your cost":"Dein Einkaufspreis","Visible":"Sichtbar","Featured":"Hervorgehoben","Create product":"Produkt erstellen","Name":"Name","Description":"Beschreibung","Slug":"Slug","Live":"Live","Hidden":"Versteckt","plans":"Tarife","Plans & pricing":"Tarife & Preise","Public price · sale price · cost":"Öffentlicher Preis · Angebot · Kosten","On":"An","Save":"Speichern","Remove this plan?":"Diesen Tarif entfernen?","New plan":"Neuer Tarif","Add plan":"Tarif hinzufügen","Remove / Archive product":"Produkt entfernen / archivieren","Offers":"Angebote","Build bundles, choose plans and set one special price.":"Bundles erstellen, Tarife wählen und einen Sonderpreis festlegen.","Bundle name":"Bundle-Name","Bundle price":"Bundle-Preis","items":"Elemente","Save bundle":"Bundle speichern","Remove this bundle?":"Dieses Bundle entfernen?","Remove":"Entfernen","No bundles yet.":"Noch keine Bundles.","Completed orders activate subscriptions and referral rewards.":"Abgeschlossene Bestellungen aktivieren Abos und Empfehlungsprämien.","Customer":"Kunde","Sale":"Verkauf","Status":"Status","No customers yet.":"Noch keine Kunden.","People":"Kunden","Trust":"Vertrauen","Reviews":"Bewertungen","Approve":"Freigeben","Reject":"Ablehnen","Approved":"Freigegeben","No reviews yet.":"Noch keine Bewertungen.","Create discounts without touching code.":"Rabatte ohne Code erstellen.","Code":"Code","Max uses":"Max. Nutzungen","Create coupon":"Gutschein erstellen","used":"verwendet","Disable":"Deaktivieren","Enable":"Aktivieren","Automation":"Automatisierung","Connect two admin Telegram IDs and receive order notifications.":"Zwei Admin-Telegram-IDs verbinden und Bestellmeldungen erhalten.","Connected":"Verbunden","Not configured":"Nicht eingerichtet","Admin 1 Telegram ID":"Telegram-ID Admin 1","Admin 2 Telegram ID":"Telegram-ID Admin 2","Save Telegram IDs":"Telegram-IDs speichern","Send test notification":"Testmeldung senden","Bot commands":"Bot-Befehle","Admin commands":"Admin-Befehle","Store":"Shop","Settings & appearance":"Einstellungen & Design","Control the look, language and public contact details.":"Aussehen, Sprache und öffentliche Kontaktdaten steuern.","Store name":"Shopname","Support WhatsApp":"Support-WhatsApp","Support email":"Support-E-Mail","Default language":"Standardsprache","Hero title – English":"Hero-Titel – Englisch","Hero title – German":"Hero-Titel – Deutsch","Hero title – French":"Hero-Titel – Französisch","Hero title – Albanian":"Hero-Titel – Albanisch","Announcement":"Ankündigung","Optional top announcement":"Optionale Ankündigung oben","Store logo URL":"Shop-Logo-URL","Default theme":"Standard-Theme","Maintenance mode":"Wartungsmodus","Save settings":"Einstellungen speichern","Available themes":"Verfügbare Themes","Security":"Sicherheit","Activity log":"Aktivitätsprotokoll","See what admins changed.":"Sieh, was Admins geändert haben.","Clear activity log?":"Aktivitätsprotokoll löschen?","Clear log":"Protokoll löschen","No activity yet.":"Noch keine Aktivität."
})
TRANSLATIONS["fr"].update({k:v for k,v in {
"Digital marketplace":"Marché numérique","Digital access. Real possibilities.":"Accès numérique. De vraies possibilités.","Premium digital products in one simple marketplace.":"Produits numériques premium sur une boutique simple.","Digital products. Better value.":"Produits numériques. Meilleure valeur.","More possibilities.":"Plus de possibilités.","Less spending.":"Moins de dépenses.","Get the digital services you use every day at better prices, with simple ordering and real support.":"Les services numériques du quotidien à de meilleurs prix, avec une commande simple et un vrai support.","Browse Products":"Voir les produits","See Deals":"Voir les offres","Clear prices":"Prix clairs","Easy ordering":"Commande simple","Real support":"Vrai support","All":"Tous","Shop":"Boutique","Popular products":"Produits populaires","Choose what you need. Keep it simple.":"Choisissez ce dont vous avez besoin. Faites simple.","products":"produits","product":"produit","Search products...":"Rechercher des produits...","month":"mois","months":"mois","Popular":"Populaire","from":"à partir de","View product":"Voir le produit","No products match your search.":"Aucun produit ne correspond à votre recherche.","Save more":"Économisez plus","Bundle deals":"Offres groupées","Put several services together and pay one special price.":"Regroupez plusieurs services et payez un prix spécial.","Special price":"Prix spécial","Get bundle":"Acheter le bundle","Login to order":"Connectez-vous pour commander","Simple ordering":"Commande simple","No unnecessary steps.":"Aucune étape inutile.","Transparent pricing":"Prix transparents","See the price before ordering.":"Voyez le prix avant de commander.","One account":"Un compte","Orders and subscriptions together.":"Commandes et abonnements au même endroit.","We are here when you need us.":"Nous sommes là quand vous en avez besoin.","A smarter way to buy":"Acheter plus intelligemment","Why pay full price when you can pay less?":"Pourquoi payer le prix fort quand vous pouvez payer moins ?","Great digital services, better value. Simple, transparent and made for everyday use.":"De grands services numériques, une meilleure valeur. Simple, transparent et pensé pour le quotidien.","Explore now":"Découvrir maintenant","Change theme":"Changer de thème","Help":"Aide","Company":"Entreprise","FAQ":"FAQ","Delivery":"Livraison","About":"À propos","Privacy":"Confidentialité","Terms":"Conditions","All rights reserved.":"Tous droits réservés.","Back to products":"Retour aux produits","Add / Remove Wishlist":"Ajouter / retirer des favoris","Choose your plan":"Choisissez votre formule","Full access for":"Accès complet pendant","Coupon":"Coupon","Order":"Commander","Customer reviews":"Avis clients","Write a review...":"Écrire un avis...","Submit Review":"Envoyer l'avis","No approved reviews yet.":"Aucun avis approuvé pour le moment.","Welcome back":"Bon retour","Sign in to manage your orders and subscriptions.":"Connectez-vous pour gérer vos commandes et abonnements.","No account yet?":"Pas encore de compte ?","Create your account":"Créez votre compte","Only username, email and password.":"Seulement nom d'utilisateur, e-mail et mot de passe.","Already have an account?":"Vous avez déjà un compte ?","Customer account":"Compte client","Active subscriptions":"Abonnements actifs","Expires":"Expire","Active":"Actif","No subscriptions yet.":"Aucun abonnement pour le moment.","Rewards":"Récompenses","Loyalty points":"Points fidélité","My referral link":"Mon lien de parrainage","Referral points are earned only after your referred friend completes a purchase.":"Les points de parrainage sont gagnés uniquement après l'achat terminé de votre filleul.","Orders":"Commandes","No orders yet.":"Aucune commande pour le moment.","Order created":"Commande créée","Your order is ready. Contact us on WhatsApp to complete it.":"Votre commande est prête. Contactez-nous sur WhatsApp pour la finaliser.","Go to account":"Aller au compte","Veyra Control":"Veyra Control","Manage everything from one place":"Gérez tout au même endroit","Dashboard":"Tableau de bord","Bundles":"Bundles","Marketing":"Marketing","Settings":"Paramètres","Activity":"Activité","View store":"Voir la boutique","Control center":"Centre de contrôle","Good control. Less work.":"Plus de contrôle. Moins de travail.","Run products, prices, offers, orders and customers without touching code.":"Gérez produits, prix, offres, commandes et clients sans toucher au code.","Product":"Produit","Bundle":"Bundle","Sales":"Ventes","Cost":"Coût","Profit":"Bénéfice","non-cancelled orders":"commandes non annulées","acquisition cost":"coût d'acquisition","sales minus cost":"ventes moins coûts","pending":"en attente","registered":"inscrits","active":"actifs","Catalog":"Catalogue","Add products, images, plans, prices and visibility from here.":"Gérez produits, images, formules, prix et visibilité ici.","Product name":"Nom du produit","Category":"Catégorie","URL slug":"Slug URL","Short description":"Courte description","Image URL":"URL de l'image","Replace image":"Remplacer l'image","Months":"Mois","Price":"Prix","Sale price":"Prix promo","Your cost":"Votre coût","Visible":"Visible","Featured":"À la une","Create product":"Créer le produit","Name":"Nom","Description":"Description","Slug":"Slug","Live":"Actif","Hidden":"Masqué","plans":"formules","Plans & pricing":"Formules & prix","Public price · sale price · cost":"Prix public · promo · coût","On":"Actif","Save":"Enregistrer","Remove this plan?":"Supprimer cette formule ?","New plan":"Nouvelle formule","Add plan":"Ajouter une formule","Remove / Archive product":"Supprimer / archiver le produit","Offers":"Offres","Build bundles, choose plans and set one special price.":"Créez des bundles, choisissez les formules et fixez un prix spécial.","Bundle name":"Nom du bundle","Bundle price":"Prix du bundle","items":"éléments","Save bundle":"Enregistrer le bundle","Remove this bundle?":"Supprimer ce bundle ?","Remove":"Supprimer","No bundles yet.":"Aucun bundle pour le moment.","Completed orders activate subscriptions and referral rewards.":"Les commandes terminées activent les abonnements et les récompenses de parrainage.","Customer":"Client","Sale":"Vente","Status":"Statut","No customers yet.":"Aucun client pour le moment.","People":"Clients","Trust":"Confiance","Reviews":"Avis","Approve":"Approuver","Reject":"Rejeter","Approved":"Approuvé","No reviews yet.":"Aucun avis.","Create discounts without touching code.":"Créez des réductions sans toucher au code.","Code":"Code","Max uses":"Utilisations max.","Create coupon":"Créer un coupon","used":"utilisé","Disable":"Désactiver","Enable":"Activer","Automation":"Automatisation","Connect two admin Telegram IDs and receive order notifications.":"Connectez deux IDs Telegram admin et recevez les notifications de commande.","Connected":"Connecté","Not configured":"Non configuré","Admin 1 Telegram ID":"ID Telegram Admin 1","Admin 2 Telegram ID":"ID Telegram Admin 2","Save Telegram IDs":"Enregistrer les IDs Telegram","Send test notification":"Envoyer une notification test","Bot commands":"Commandes du bot","Admin commands":"Commandes admin","Store":"Boutique","Settings & appearance":"Paramètres & apparence","Control the look, language and public contact details.":"Gérez l'apparence, la langue et les contacts publics.","Store name":"Nom de la boutique","Support WhatsApp":"WhatsApp support","Support email":"E-mail support","Default language":"Langue par défaut","Hero title – English":"Titre hero – anglais","Hero title – German":"Titre hero – allemand","Hero title – French":"Titre hero – français","Hero title – Albanian":"Titre hero – albanais","Announcement":"Annonce","Optional top announcement":"Annonce supérieure facultative","Store logo URL":"URL du logo","Default theme":"Thème par défaut","Maintenance mode":"Mode maintenance","Save settings":"Enregistrer les paramètres","Available themes":"Thèmes disponibles","Security":"Sécurité","Activity log":"Journal d'activité","See what admins changed.":"Voir ce que les admins ont modifié.","Clear activity log?":"Effacer le journal d'activité ?","Clear log":"Effacer le journal","No activity yet.":"Aucune activité."
}.items()})
TRANSLATIONS["sq"].update({k:v for k,v in {
"Digital marketplace":"Treg digjital","Digital access. Real possibilities.":"Qasje digjitale. Mundësi reale.","Premium digital products in one simple marketplace.":"Produkte digjitale premium në një treg të thjeshtë.","Digital products. Better value.":"Produkte digjitale. Vlerë më e mirë.","More possibilities.":"Më shumë mundësi.","Less spending.":"Më pak shpenzime.","Get the digital services you use every day at better prices, with simple ordering and real support.":"Merr shërbimet digjitale që përdor çdo ditë me çmime më të mira, porosi të thjeshtë dhe mbështetje reale.","Browse Products":"Shiko produktet","See Deals":"Shiko ofertat","Clear prices":"Çmime të qarta","Easy ordering":"Porosi e lehtë","Real support":"Mbështetje reale","All":"Të gjitha","Shop":"Dyqani","Popular products":"Produktet e njohura","Choose what you need. Keep it simple.":"Zgjidh çfarë të duhet. Mbaje të thjeshtë.","products":"produkte","product":"produkt","Search products...":"Kërko produkte...","month":"muaj","months":"muaj","Popular":"Popullore","from":"nga","View product":"Shiko produktin","No products match your search.":"Asnjë produkt nuk përputhet me kërkimin.","Save more":"Kursen më shumë","Bundle deals":"Oferta Bundle","Put several services together and pay one special price.":"Bashko disa shërbime dhe paguaj një çmim special.","Special price":"Çmim special","Get bundle":"Bli Bundle","Login to order":"Hyr për të porositur","Simple ordering":"Porosi e thjeshtë","No unnecessary steps.":"Pa hapa të panevojshëm.","Transparent pricing":"Çmime transparente","See the price before ordering.":"Shiko çmimin para porosisë.","One account":"Një llogari","Orders and subscriptions together.":"Porositë dhe abonimet në një vend.","We are here when you need us.":"Jemi këtu kur të duhemi.","A smarter way to buy":"Mënyrë më e zgjuar për të blerë","Why pay full price when you can pay less?":"Pse të paguash çmimin e plotë kur mund të paguash më pak?","Great digital services, better value. Simple, transparent and made for everyday use.":"Shërbime të mira digjitale, vlerë më e mirë. Të thjeshta, transparente dhe për përdorim të përditshëm.","Explore now":"Eksploro tani","Change theme":"Ndrysho temën","Help":"Ndihmë","Company":"Kompania","FAQ":"FAQ","Delivery":"Dorëzimi","About":"Rreth nesh","Privacy":"Privatësia","Terms":"Kushtet","All rights reserved.":"Të gjitha të drejtat e rezervuara.","Back to products":"Kthehu te produktet","Add / Remove Wishlist":"Shto / hiq nga dëshirat","Choose your plan":"Zgjidh planin","Full access for":"Qasje e plotë për","Coupon":"Kupon","Order":"Porosit","Customer reviews":"Vlerësimet e klientëve","Write a review...":"Shkruaj vlerësim...","Submit Review":"Dërgo vlerësimin","No approved reviews yet.":"Ende nuk ka vlerësime të aprovuara.","Welcome back":"Mirë se u ktheve","Sign in to manage your orders and subscriptions.":"Hyr për të menaxhuar porositë dhe abonimet.","No account yet?":"Nuk ke llogari?","Create your account":"Krijo llogarinë","Only username, email and password.":"Vetëm username, email dhe fjalëkalim.","Already have an account?":"Ke llogari?","Customer account":"Llogaria e klientit","Active subscriptions":"Abonimet aktive","Expires":"Skadon","Active":"Aktiv","No subscriptions yet.":"Ende nuk ka abonime.","Rewards":"Shpërblimet","Loyalty points":"Pikët e besnikërisë","My referral link":"Linku im i referimit","Referral points are earned only after your referred friend completes a purchase.":"Pikët e referimit fitohen vetëm pasi personi i referuar e përfundon blerjen.","Orders":"Porositë","No orders yet.":"Ende nuk ka porosi.","Order created":"Porosia u krijua","Your order is ready. Contact us on WhatsApp to complete it.":"Porosia jote është gati. Na kontakto në WhatsApp për ta përfunduar.","Go to account":"Shko te llogaria","Veyra Control":"Veyra Control","Manage everything from one place":"Menaxho gjithçka nga një vend","Dashboard":"Paneli","Bundles":"Bundles","Marketing":"Marketing","Settings":"Cilësimet","Activity":"Aktiviteti","View store":"Shiko dyqanin","Control center":"Qendra e kontrollit","Good control. Less work.":"Më shumë kontroll. Më pak punë.","Run products, prices, offers, orders and customers without touching code.":"Menaxho produktet, çmimet, ofertat, porositë dhe klientët pa prekur kodin.","Product":"Produkt","Bundle":"Bundle","Sales":"Shitjet","Cost":"Kosto","Profit":"Fitimi","non-cancelled orders":"porosi jo të anuluara","acquisition cost":"kosto e blerjes","sales minus cost":"shitje minus kosto","pending":"në pritje","registered":"të regjistruar","active":"aktive","Catalog":"Katalogu","Add products, images, plans, prices and visibility from here.":"Shto produkte, foto, plane, çmime dhe kontrollo dukshmërinë këtu.","Product name":"Emri i produktit","Category":"Kategoria","URL slug":"Slug i URL-së","Short description":"Përshkrim i shkurtër","Image URL":"URL e fotos","Replace image":"Zëvendëso foton","Months":"Muajt","Price":"Çmimi","Sale price":"Çmimi në zbritje","Your cost":"Kostoja jote","Visible":"I dukshëm","Featured":"I veçuar","Create product":"Krijo produkt","Name":"Emri","Description":"Përshkrimi","Slug":"Slug","Live":"Aktiv","Hidden":"Fshehur","plans":"plane","Plans & pricing":"Planet & çmimet","Public price · sale price · cost":"Çmimi publik · zbritja · kostoja","On":"Aktiv","Save":"Ruaj","Remove this plan?":"Ta heqim këtë plan?","New plan":"Plan i ri","Add plan":"Shto plan","Remove / Archive product":"Hiq / Arkivo produktin","Offers":"Ofertat","Build bundles, choose plans and set one special price.":"Krijo bundles, zgjidh planet dhe vendos një çmim special.","Bundle name":"Emri i Bundle","Bundle price":"Çmimi i Bundle","items":"artikuj","Save bundle":"Ruaj Bundle","Remove this bundle?":"Ta heqim këtë Bundle?","Remove":"Hiq","No bundles yet.":"Ende nuk ka Bundles.","Completed orders activate subscriptions and referral rewards.":"Porositë e përfunduara aktivizojnë abonimet dhe shpërblimet e referimit.","Customer":"Klienti","Sale":"Shitja","Status":"Statusi","No customers yet.":"Ende nuk ka klientë.","People":"Klientët","Trust":"Besimi","Reviews":"Vlerësimet","Approve":"Aprovo","Reject":"Refuzo","Approved":"Aprovuar","No reviews yet.":"Ende nuk ka vlerësime.","Create discounts without touching code.":"Krijo zbritje pa prekur kodin.","Code":"Kodi","Max uses":"Përdorime max.","Create coupon":"Krijo kupon","used":"përdorur","Disable":"Çaktivizo","Enable":"Aktivizo","Automation":"Automatizimi","Connect two admin Telegram IDs and receive order notifications.":"Lidh dy ID të adminëve në Telegram dhe merr njoftime për porositë.","Connected":"I lidhur","Not configured":"Nuk është konfiguruar","Admin 1 Telegram ID":"Telegram ID Admin 1","Admin 2 Telegram ID":"Telegram ID Admin 2","Save Telegram IDs":"Ruaj Telegram ID-të","Send test notification":"Dërgo njoftim testues","Bot commands":"Komandat e botit","Admin commands":"Komandat e adminit","Store":"Dyqani","Settings & appearance":"Cilësimet & pamja","Control the look, language and public contact details.":"Kontrollo pamjen, gjuhën dhe kontaktet publike.","Store name":"Emri i dyqanit","Support WhatsApp":"WhatsApp i supportit","Support email":"Email i supportit","Default language":"Gjuha fillestare","Hero title – English":"Titulli hero – Anglisht","Hero title – German":"Titulli hero – Gjermanisht","Hero title – French":"Titulli hero – Frëngjisht","Hero title – Albanian":"Titulli hero – Shqip","Announcement":"Njoftimi","Optional top announcement":"Njoftim opsional në krye","Store logo URL":"URL e logos së dyqanit","Default theme":"Tema fillestare","Maintenance mode":"Modalitet mirëmbajtjeje","Save settings":"Ruaj cilësimet","Available themes":"Temat në dispozicion","Security":"Siguria","Activity log":"Ditari i aktivitetit","See what admins changed.":"Shiko çfarë kanë ndryshuar adminët.","Clear activity log?":"Ta pastrojmë ditarin e aktivitetit?","Clear log":"Pastro ditarin","No activity yet.":"Ende nuk ka aktivitet."
}.items()})

for _lang, _vals in {
    "en": {"month":"month","year":"year","subscription":"subscription","one-time":"one-time","per month":"per month","per year":"per year"},
    "de": {"month":"Monat","year":"Jahr","subscription":"Abonnement","one-time":"einmalig","per month":"pro Monat","per year":"pro Jahr"},
    "fr": {"month":"mois","year":"an","subscription":"abonnement","one-time":"paiement unique","per month":"par mois","per year":"par an"},
    "sq": {"month":"muaj","year":"vit","subscription":"abonim","one-time":"një herë","per month":"në muaj","per year":"në vit"},
}.items():
    TRANSLATIONS[_lang].update(_vals)

for _l in ("de", "fr", "sq"):
    TRANSLATIONS[_l].update({"Customer account": TRANSLATIONS[_l].get("Account","Account"),"Started":"Gestartet am" if _l=="de" else ("Commencé le" if _l=="fr" else "Filluar më"),"No approved reviews yet.":"Noch keine freigegebenen Bewertungen." if _l=="de" else ("Aucun avis approuvé pour le moment." if _l=="fr" else "Ende nuk ka vlerësime të aprovuara."),"Back to products":"Zurück zu den Produkten" if _l=="de" else ("Retour aux produits" if _l=="fr" else "Kthehu te produktet"),"Add / Remove Wishlist":"Zur Wunschliste hinzufügen / entfernen" if _l=="de" else ("Ajouter / retirer des favoris" if _l=="fr" else "Shto / hiq nga dëshirat"),"Full access for":"Voller Zugriff für" if _l=="de" else ("Accès complet pendant" if _l=="fr" else "Qasje e plotë për"),"Protected admin area":"Geschützter Admin-Bereich" if _l=="de" else ("Espace admin protégé" if _l=="fr" else "Zona e mbrojtur e adminit"),"Code":"Code" if _l in ("de","fr") else "Kodi","Discount":"Rabatt" if _l=="de" else ("Réduction" if _l=="fr" else "Zbritja"),"Uses":"Nutzungen" if _l=="de" else ("Utilisations" if _l=="fr" else "Përdorime"),"Expires":"Läuft ab" if _l=="de" else ("Expire" if _l=="fr" else "Skadon"),"Create account":"Konto erstellen" if _l=="de" else ("Créer un compte" if _l=="fr" else "Krijo llogari"),"Open WhatsApp":"WhatsApp öffnen" if _l=="de" else ("Ouvrir WhatsApp" if _l=="fr" else "Hap WhatsApp")})

# Admin UX vocabulary.
for _lang, _vals in {
    "en": {"Storefront text":"Storefront text","Display order":"Display order","Drag to reorder":"Drag to reorder","Full description":"Full description","Save this change":"Save this change"},
    "de": {"Storefront text":"Shop-Text","Display order":"Anzeigereihenfolge","Drag to reorder":"Zum Sortieren ziehen","Full description":"Vollständige Beschreibung","Save this change":"Änderung speichern"},
    "fr": {"Storefront text":"Texte boutique","Display order":"Ordre d'affichage","Drag to reorder":"Glisser pour réorganiser","Full description":"Description complète","Save this change":"Enregistrer"},
    "sq": {"Storefront text":"Teksti në pamjen e dyqanit","Display order":"Renditja e shfaqjes","Drag to reorder":"Tërhiq për ta renditur","Full description":"Përshkrimi i plotë","Save this change":"Ruaj ndryshimin"}
}.items():
    TRANSLATIONS[_lang].update(_vals)

# Category labels are translated with the rest of the storefront.
for _lang, _vals in {
    "en": {"Streaming":"Streaming","Music":"Music","AI":"AI","Sport":"Sport","Gaming":"Gaming","Software":"Software","Cloud":"Cloud","VPN & Security":"VPN & Security"},
    "de": {"Streaming":"Streaming","Music":"Musik","AI":"KI","Sport":"Sport","Gaming":"Gaming","Software":"Software","Cloud":"Cloud","VPN & Security":"VPN & Sicherheit"},
    "fr": {"Streaming":"Streaming","Music":"Musique","AI":"IA","Sport":"Sport","Gaming":"Jeux","Software":"Logiciels","Cloud":"Cloud","VPN & Security":"VPN & sécurité"},
    "sq": {"Streaming":"Streaming","Music":"Muzikë","AI":"AI","Sport":"Sport","Gaming":"Gaming","Software":"Software","Cloud":"Cloud","VPN & Security":"VPN & Siguri"},
}.items():
    TRANSLATIONS[_lang].update(_vals)

for _lang, _vals in {
    "en":{"Premium":"Premium","Made simple.":"Made simple.","More":"More","Special offer":"Special offer","Better prices. Simple choice.":"Better prices. Simple choice.","See the products customers choose most.":"See the products customers choose most.","Explore":"Explore","View products":"View products","Find your next favorite.":"Find your next favorite.","Pay securely":"Pay securely"},
    "de":{"Premium":"Premium","Made simple.":"Einfach gemacht.","More":"Mehr","Special offer":"Sonderangebot","Better prices. Simple choice.":"Bessere Preise. Einfache Wahl.","See the products customers choose most.":"Entdecke die beliebtesten Produkte.","Explore":"Entdecken","View products":"Produkte ansehen","Find your next favorite.":"Finde deinen nächsten Favoriten.","Pay securely":"Sicher bezahlen"},
    "fr":{"Premium":"Premium","Made simple.":"En toute simplicité.","More":"Plus","Special offer":"Offre spéciale","Better prices. Simple choice.":"De meilleurs prix. Un choix simple.","See the products customers choose most.":"Découvrez les produits les plus choisis.","Explore":"Explorer","View products":"Voir les produits","Find your next favorite.":"Trouvez votre prochain favori.","Pay securely":"Payer en toute sécurité"},
    "sq":{"Premium":"Premium","Made simple.":"Thjesht.","More":"Më shumë","Special offer":"Ofertë speciale","Better prices. Simple choice.":"Çmime më të mira. Zgjedhje e thjeshtë.","See the products customers choose most.":"Shiko produktet që zgjedhin më shumë klientët.","Explore":"Eksploro","View products":"Shiko produktet","Find your next favorite.":"Gjej të preferuarin tënd të radhës.","Pay securely":"Paguaj në mënyrë të sigurt"},
}.items(): TRANSLATIONS[_lang].update(_vals)

PRODUCT_LOGOS = {
    "netflix":"/static/logos/netflix.svg",
    "spotify":"/static/logos/spotify.svg",
    "youtube-premium":"/static/logos/youtube-premium.svg",
    "crunchyroll-mega-fan":"/static/logos/crunchyroll-mega-fan.svg",
    "chatgpt":"/static/logos/chatgpt.svg",
    "gemini":"/static/logos/gemini.svg",
    "dazn":"/static/logos/dazn.svg",
    "disney-plus":"/static/logos/disney-plus.svg",
    "amazon-prime":"/static/logos/amazon-prime.svg",
    "apple-tv-plus":"/static/logos/apple-tv-plus.svg",
    "apple-music":"/static/logos/apple-music.svg",
    "canva-pro":"/static/logos/canva-pro.svg",
    "capcut-pro":"/static/logos/capcut-pro.svg",
    "microsoft-365":"/static/logos/microsoft-365.svg",
    "google-one":"/static/logos/google-one.svg",
    "icloud-plus":"/static/logos/icloud-plus.svg",
    "nordvpn":"/static/logos/nordvpn.svg",
    "xbox-game-pass":"/static/logos/xbox-game-pass.svg",
    "playstation-plus":"/static/logos/playstation-plus.svg",
}

def product_logo(product):
    # Known brands always use bundled local assets. This prevents broken
    # external image URLs from replacing the real brand mark. Unknown/admin
    # products can still use their custom image URL.
    mapped = PRODUCT_LOGOS.get(product.slug, "")
    if mapped:
        return mapped + "?v=20260910"
    return (product.image_url or "").strip()


def public_order_ref(order):
    """Stable non-sequential customer-facing order reference."""
    secret = str(app.config.get("SECRET_KEY", "veyra"))
    digest = hashlib.sha256(f"{secret}:{order.id}".encode("utf-8")).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=")[:10]
    return f"VYR-{token}"

@app.before_request
def set_language():
    lang = request.args.get("lang")
    if lang in TRANSLATIONS:
        session["lang"] = lang
    if "lang" not in session:
        session["lang"] = "en"
    if request.endpoint not in {"static", "login", "register", "logout"} and "AdminSetting" in globals():
        try:
            row = AdminSetting.query.filter_by(key="maintenance_mode").first()
            if row and row.value == "on" and not (current_user.is_authenticated and current_user.is_admin):
                return render_template("maintenance.html"), 503
        except Exception:
            pass

@app.context_processor
def inject_i18n():
    lang = session.get("lang", "en")
    def tr(text):
        return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(text, TRANSLATIONS["en"].get(text, text))
    settings = {x.key:x.value for x in AdminSetting.query.all()} if 'AdminSetting' in globals() else {}
    theme = settings.get("default_theme", "clean")
    hero_defaults = {"en":"More possibilities. Less spending.","de":"Mehr Möglichkeiten. Weniger Ausgaben.","fr":"Plus de possibilités. Moins de dépenses.","sq":"Më shumë mundësi. Më pak shpenzime."}
    hero_title = settings.get(f"hero_title_{lang}") or tr(hero_defaults[lang])
    def switch_url(code):
        from urllib.parse import urlencode
        args = request.args.to_dict(flat=True)
        args["lang"] = code
        return request.path + ("?" + urlencode(args) if args else "") + ("#" + request.path.split("#",1)[1] if "#" in request.path else "")
    return {"public_order_ref": public_order_ref, "lang": lang, "languages": [("en","English"),("de","Deutsch"),("fr","Français"),("sq","Shqip")], "tr": tr, "translations": TRANSLATIONS, "switch_url": switch_url, "store_name": settings.get("store_name","Veyra"), "theme": theme, "announcement": settings.get("announcement",""), "hero_title": hero_title, "store_logo_url": settings.get("store_logo_url",""), "payment_provider": settings.get("payment_provider","whatsapp"), "product_logo": product_logo}

REFERENCE_DESCRIPTIONS = {
    "spotify": "Individual access with account upgrade. Family Share Individual is also available. Choose the duration that fits you.",
    "disney-plus": "Shared account with 1 profile. Available in different durations for simple access to Disney+.",
    "netflix": "4K Ultra account with 1 shared profile and PIN protection. Available for 1, 2 or 3 months.",
    "gemini": "18-month activation linked to your personal account. Long-term access without a separate shared profile.",
    "crunchyroll-mega-fan": "Mega Fan account upgrade for your personal account, with shared-profile and PIN-protection options depending on the plan.",
    "dazn": "DAZN Ultimate access with country-specific options shown by plan. Available for England, Spain, Italy, Germany and France.",
    "amazon-prime": "Amazon Prime account access with 3, 6 or 12-month options.",
    "quillbot": "Private QuillBot account for individual use. Available as a simple long-term option.",
    "deezer": "Deezer account access with a 12-month option.",
    "ipvanish": "IPVanish VPN account with 12-month access for private and secure browsing.",
    "expressvpn": "ExpressVPN account with 12-month access for private browsing and secure connections.",
    "nordvpn": "NordVPN account with 12-month access for private browsing and secure connections.",
    "windows": "Windows Pro product keys. Choose between Windows 11 Pro and Windows 10 Pro keys.",
    "microsoft-365": "Microsoft 365 Pro Plus key with a 1-year option. Includes the familiar Office applications for productivity.",
}

def seed():
    cats = [
        ("Streaming","streaming"),("Music","music"),("AI","ai"),("Sport","sport"),
        ("Gaming","gaming"),("Software","software"),("Cloud","cloud"),("VPN & Security","vpn-security")
    ]
    for name, slug in cats:
        if not Category.query.filter_by(slug=slug).first():
            db.session.add(Category(name=name, slug=slug))
    db.session.commit()

    defaults = [
        ("Netflix","netflix","Streaming"),
        ("Spotify","spotify","Music"),
        ("YouTube Premium","youtube-premium","Streaming"),
        ("Crunchyroll Mega Fan","crunchyroll-mega-fan","Streaming"),
        ("ChatGPT","chatgpt","AI"),
        ("Google AI Pro / Gemini","gemini","AI"),
        ("DAZN","dazn","Sport"),
        ("Disney+","disney-plus","Streaming"),
        ("Amazon Prime","amazon-prime","Streaming"),
        ("Apple TV+","apple-tv-plus","Streaming"),
        ("Apple Music","apple-music","Music"),
        ("Canva Pro","canva-pro","Software"),
        ("CapCut Pro","capcut-pro","Software"),
        ("Microsoft 365","microsoft-365","Software"),
        ("Google One","google-one","Cloud"),
        ("iCloud+","icloud-plus","Cloud"),
        ("NordVPN","nordvpn","VPN & Security"),
        ("Xbox Game Pass","xbox-game-pass","Gaming"),
        ("PlayStation Plus","playstation-plus","Gaming"),
    ]
    for name, slug, cat_name in defaults:
        if not Product.query.filter_by(slug=slug).first():
            cat = Category.query.filter_by(name=cat_name).first()
            p = Product(name=name, slug=slug, category=cat, description=REFERENCE_DESCRIPTIONS.get(slug, f"{name} subscription."))
            db.session.add(p)
            db.session.flush()
            for months, label in [(1,"1 Month"),(3,"3 Months"),(6,"6 Months"),(12,"12 Months")]:
                db.session.add(Plan(product_id=p.id, name=label, months=months, price=0))
    # Use the supplied reference image only as a content reference for descriptions.
    # Never use the uploaded image as a storefront/product image.
    desc_changed = False
    for _slug, _desc in REFERENCE_DESCRIPTIONS.items():
        _p = Product.query.filter_by(slug=_slug).first()
        if _p and (not (_p.description or '').strip() or (_p.description or '').strip() == f"{_p.name} subscription."):
            _p.description = _desc
            desc_changed = True
    if desc_changed:
        db.session.commit()

    # Give seeded products polished brand logos automatically; admin-uploaded images always win.
    changed = False
    for _p in Product.query.all():
        if not _p.image_url and _p.slug in PRODUCT_LOGOS:
            _p.image_url = PRODUCT_LOGOS[_p.slug]
            changed = True
    if changed:
        db.session.commit()

    # Example bulk offer. Admin can edit/add bundles later.
    if not Bundle.query.filter_by(name="Netflix + Spotify Bundle").first():
        netflix = Product.query.filter_by(slug="netflix").first()
        spotify = Product.query.filter_by(slug="spotify").first()
        if netflix and spotify:
            nplan = Plan.query.filter_by(product_id=netflix.id, months=1).first()
            splan = Plan.query.filter_by(product_id=spotify.id, months=1).first()
            if nplan and splan:
                b = Bundle(name="Netflix + Spotify Bundle",
                           description="Get Netflix + Spotify together for a lower price.",
                           price=8.0, featured=True)
                db.session.add(b); db.session.flush()
                db.session.add(BundleItem(bundle_id=b.id, plan_id=nplan.id, quantity=1))
                db.session.add(BundleItem(bundle_id=b.id, plan_id=splan.id, quantity=1))
                db.session.commit()

    for uname, env_user, env_pass in [
        ("admin1","ADMIN1_USERNAME","ADMIN1_PASSWORD"),
        ("admin2","ADMIN2_USERNAME","ADMIN2_PASSWORD")
    ]:
        username = os.getenv(env_user, uname)
        password = os.getenv(env_pass, "")
        if password and not User.query.filter_by(username=username).first():
            db.session.add(User(username=username, email=f"{username}@local.admin",
                                password_hash=generate_password_hash(password), is_admin=True))
    db.session.commit()

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def admin_required():
    return current_user.is_authenticated and current_user.is_admin

@app.context_processor
def inject_now():
    return {"now": datetime.utcnow()}

def apply_coupon(code, subtotal, user_id=None, plan_id=None):
    if not code:
        return 0.0, None, ""
    c = Coupon.query.filter_by(code=code.strip().upper(), active=True).first()
    if not c:
        return 0.0, None, "Invalid coupon code."
    if c.expires_at and c.expires_at < datetime.utcnow():
        return 0.0, None, "This coupon has expired."
    if c.max_uses and c.used_count >= c.max_uses:
        return 0.0, None, "This coupon has reached its usage limit."
    if user_id and CouponUsage.query.filter_by(coupon_id=c.id, user_id=user_id).first():
        return 0.0, None, "You have already used this coupon."
    if "V21CouponRule" in globals():
        rule = V21CouponRule.query.filter_by(coupon_id=c.id).first()
        if rule and rule.enabled:
            plan_obj = db.session.get(Plan, plan_id) if plan_id else None
            if rule.product_id and (not plan_obj or plan_obj.product_id != rule.product_id):
                return 0.0, None, "This coupon does not apply to this product."
            if rule.category_id and (not plan_obj or plan_obj.product.category_id != rule.category_id):
                return 0.0, None, "This coupon does not apply to this category."
    discount = subtotal * (c.discount_percent / 100.0) + c.discount_fixed
    return max(0.0, min(subtotal, discount)), c, ""

def loyalty_setting(key, default):
    row = AdminSetting.query.filter_by(key=key).first()
    try:
        return float(row.value) if row and row.value != "" else float(default)
    except (TypeError, ValueError):
        return float(default)


def telegram_admin_ids():
    """Return the exact two configured admin Telegram numeric IDs."""
    ids = []
    for key in ("ADMIN1_TELEGRAM_ID", "ADMIN2_TELEGRAM_ID"):
        value = os.getenv(key, "").strip()
        if value and value not in ids:
            ids.append(value)
    # DB values are useful after the admin saves them in the panel.
    def add_db_ids():
        for u in User.query.filter_by(is_admin=True).order_by(User.id).limit(2).all():
            value = (u.telegram_id or "").strip()
            if value and value not in ids:
                ids.append(value)
    if has_app_context():
        add_db_ids()
    else:
        try:
            with app.app_context():
                add_db_ids()
        except Exception:
            pass
    return ids[:2]

def telegram_authorized(user_id):
    ids = telegram_admin_ids()
    return len(ids) == 2 and str(user_id) in ids

def telegram_api(method, payload=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {}, timeout=20)
        if r.ok:
            return r.json()
        app.logger.warning("Telegram API %s failed: %s", method, r.text[:500])
    except Exception as exc:
        app.logger.warning("Telegram API %s error: %s", method, exc)
    return None

def send_telegram(message, reply_markup=None):
    ids = telegram_admin_ids()
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or len(ids) != 2:
        return False, "Telegram bot token and exactly two admin Telegram IDs are required."
    ok = False
    errors = []
    for chat_id in ids:
        payload = {"chat_id": chat_id, "text": message}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = telegram_api("sendMessage", payload)
        if result and result.get("ok"):
            ok = True
        else:
            errors.append(chat_id)
    return ok, ", ".join(errors)

def send_telegram_chat(chat_id, message, reply_markup=None):
    payload = {"chat_id": str(chat_id), "text": message}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = telegram_api("sendMessage", payload)
    return bool(result and result.get("ok"))

def sync_telegram_profile(user, tg_user, chat_id=None):
    """Persist Telegram username/name/ID for a linked Veyra customer."""
    if not user or not tg_user:
        return
    tid = str(chat_id if chat_id is not None else tg_user.get("id", "")).strip()
    if not tid:
        return
    profile = TelegramProfile.query.filter_by(user_id=user.id).first()
    if not profile:
        profile = TelegramProfile(user_id=user.id, telegram_id=tid)
        db.session.add(profile)
    profile.telegram_id = tid
    profile.username = tg_user.get("username") or None
    profile.first_name = tg_user.get("first_name") or None
    profile.last_name = tg_user.get("last_name") or None
    user.telegram_id = tid

def customer_telegram_label(user):
    if not user or not user.telegram_id:
        return "❌ Not connected"
    profile = TelegramProfile.query.filter_by(user_id=user.id).first()
    if profile and profile.username:
        return f"@{profile.username} (ID: {profile.telegram_id})"
    return f"ID: {user.telegram_id}"

def customer_order_notification(o):
    item = f"{o.plan.product.name} — {o.plan.name}" if o.plan else (o.bundle.name if o.bundle else "Order")
    return (f"🛒 NEW VEYRA ORDER #{o.id}\n\n"
            f"👤 Customer: @{o.user.username}\n"
            f"📧 Email: {o.user.email}\n"
            f"📱 Telegram: {customer_telegram_label(o.user)}\n\n"
            f"📦 Product: {item}\n"
            f"💰 Sale: €{o.sale_price:.2f}\n"
            f"💵 Cost: €{o.cost_price:.2f}\n"
            f"📈 Profit: €{o.profit:.2f}\n"
            f"🟡 Status: {o.status.upper()}")

def send_whatsapp_admin_notification(message):
    """Optional WhatsApp Cloud API notification for the store admin.
    Requires an approved template with one {{1}} body variable.
    """
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    admin_number = re.sub(r"\D", "", os.getenv("WHATSAPP_ADMIN_NUMBER", ""))
    template = os.getenv("WHATSAPP_TEMPLATE_NAME", "veyra_new_order").strip()
    language = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE_CODE", "en_US").strip()
    version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0").strip()
    if not (token and phone_id and admin_number and template):
        return False
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": admin_number,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": language},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": message[:4000]}]
            }]
        }
    }
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload, timeout=20)
        if r.ok:
            return True
        app.logger.warning("WhatsApp admin notification failed: %s", r.text[:500])
    except Exception as exc:
        app.logger.warning("WhatsApp admin notification error: %s", exc)
    return False

def configure_telegram_commands():
    """Set customer commands by default and admin commands only for the 2 admin chats."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    customer_commands = [
        {"command":"start","description":"Open Veyra"},
        {"command":"products","description":"Browse products"},
        {"command":"deals","description":"View deals"},
        {"command":"orders","description":"My orders"},
        {"command":"subscriptions","description":"My subscriptions"},
        {"command":"wishlist","description":"My wishlist"},
        {"command":"account","description":"My account"},
        {"command":"support","description":"Contact support"},
    ]
    admin_commands = [
        {"command":"admin","description":"Admin panel"},
        {"command":"products","description":"Active products"},
        {"command":"orders","description":"Pending orders"},
        {"command":"customers","description":"Customers"},
        {"command":"subscriptions","description":"Active subscriptions"},
        {"command":"stats","description":"Store stats"},
        {"command":"deliver","description":"Deliver order"},
        {"command":"cancel","description":"Cancel delivery draft"},
    ]
    telegram_api("setMyCommands", {"commands": customer_commands})
    for admin_id in telegram_admin_ids():
        telegram_api("setMyCommands", {
            "commands": admin_commands,
            "scope": {"type": "chat", "chat_id": int(admin_id)}
        })

def telegram_orders_keyboard():
    with app.app_context():
        orders = Order.query.filter(Order.status.in_(["pending", "processing"])).order_by(Order.created_at.asc()).limit(20).all()
        rows = []
        for o in orders:
            label = f"📦 #{o.id} · {(o.plan.product.name if o.plan else o.bundle.name)[:28]}"
            rows.append([{"text": label, "callback_data": f"deliver:{o.id}"}])
        return {"inline_keyboard": rows} if rows else None

# In-memory state is only the short-lived Telegram compose step; the final delivery is persisted in DB.
TELEGRAM_DELIVERY_DRAFTS = {}

def customer_telegram_user(chat_id):
    return User.query.filter_by(telegram_id=str(chat_id), is_admin=False).first()

def customer_orders_keyboard(user_id):
    orders = Order.query.filter_by(user_id=user_id).order_by(Order.created_at.desc()).limit(15).all()
    rows=[]
    for o in orders:
        item = f"{o.plan.product.name} — {o.plan.name}" if o.plan else (o.bundle.name if o.bundle else "Order")
        rows.append([{"text": f"#{o.id} · {item[:28]}", "callback_data": f"custorder:{o.id}"}])
    return {"inline_keyboard": rows} if rows else None

def customer_products_keyboard():
    products = Product.query.filter_by(active=True).order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name).limit(30).all()
    rows = []
    for p in products:
        plans = [x for x in p.plans if x.active]
        if plans:
            rows.append([{"text": f"🛍 {p.name}", "callback_data": f"custproduct:{p.id}"}])
    return {"inline_keyboard": rows} if rows else None

def customer_plan_keyboard(product_id):
    p = db.session.get(Product, product_id)
    if not p or not p.active:
        return None
    rows = []
    for plan in sorted([x for x in p.plans if x.active], key=lambda x: (x.sort_order, x.months, x.id)):
        price = plan.sale_price if plan.sale_price is not None else plan.price
        rows.append([{"text": f"📅 {plan.name} — €{price:.2f}", "callback_data": f"buyplan:{plan.id}"}])
    rows.append([{"text": "⬅️ Products", "callback_data": "cust_products"}])
    return {"inline_keyboard": rows} if rows else None

def send_customer_products(chat_id):
    kb = customer_products_keyboard()
    if kb:
        send_telegram_chat(chat_id, "🛍 Veyra Products\n\nChoose a product to see plans and buy:", kb)
    else:
        send_telegram_chat(chat_id, "📭 No products are available right now.")

def create_telegram_order(user, plan):
    selling_price = plan.sale_price if plan.sale_price is not None else plan.price
    cost = plan.cost_price or 0.0
    o = Order(user_id=user.id, plan_id=plan.id, sale_price=selling_price, cost_price=cost, profit=selling_price-cost)
    db.session.add(o)
    db.session.flush()
    audit_note = f"{public_order_ref(o)} created from Telegram for @{user.username} — {plan.product.name} / {plan.name}"
    app.logger.info(audit_note)
    db.session.commit()
    notify_admins(customer_order_notification(o), "new_order", o)
    return o

def customer_subscriptions_text(user_id):
    subs=Subscription.query.filter_by(user_id=user_id).order_by(Subscription.expires_at.desc()).all()
    if not subs: return "📭 No subscriptions yet."
    lines=["🔐 Your subscriptions"]
    for x in subs:
        state="ACTIVE" if x.active and x.expires_at>datetime.utcnow() else "EXPIRED"
        lines.append(f"• {x.product.name} — {state} — expires {x.expires_at.strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)

def customer_order_text(o):
    item=f"{o.plan.product.name} — {o.plan.name}" if o.plan else (o.bundle.name if o.bundle else "Order")
    text=f"🧾 {public_order_ref(o)}\n📦 {item}\n💶 €{o.sale_price:.2f}\n📌 {o.status}"
    if o.delivery:
        text += f"\n\n📦 Delivery\n{o.delivery.content}"
    return text

def telegram_order_text(o):
    item = f"{o.plan.product.name} — {o.plan.name}" if o.plan else o.bundle.name
    return (f"🧾 {public_order_ref(o)}\n"
            f"👤 @{o.user.username}\n"
            f"📦 {item}\n"
            f"💶 €{o.sale_price:.2f}\n"
            f"📌 Status: {o.status}")

def telegram_handle_update(update):
    # Callback buttons
    cq = update.get("callback_query") or {}
    if cq:
        uid = cq.get("from", {}).get("id")
        data = cq.get("data", "")
        chat_id = cq.get("message", {}).get("chat", {}).get("id", uid)
        if data.startswith("custorder:"):
            with app.app_context():
                try: oid=int(data.split(":",1)[1])
                except ValueError: oid=0
                customer=customer_telegram_user(chat_id)
                o=db.session.get(Order, oid)
                if not customer or not o or o.user_id != customer.id:
                    send_telegram_chat(chat_id, "⛔ Order not available.")
                else:
                    send_telegram_chat(chat_id, customer_order_text(o))
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if data == "cust_products":
            with app.app_context():
                customer = customer_telegram_user(chat_id)
                if not customer:
                    send_telegram_chat(chat_id, "⛔ Connect your Veyra account first.")
                else:
                    send_customer_products(chat_id)
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if data.startswith("custproduct:"):
            with app.app_context():
                customer = customer_telegram_user(chat_id)
                try:
                    product_id = int(data.split(":", 1)[1])
                except ValueError:
                    product_id = 0
                product = db.session.get(Product, product_id)
                kb = customer_plan_keyboard(product_id) if customer else None
                if not customer:
                    send_telegram_chat(chat_id, "⛔ Connect your Veyra account first.")
                elif not product or not product.active or not kb:
                    send_telegram_chat(chat_id, "❌ Product is no longer available.")
                else:
                    send_telegram_chat(chat_id, f"📦 {product.name}\n\nChoose your plan:", kb)
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if data.startswith("buyplan:"):
            with app.app_context():
                customer = customer_telegram_user(chat_id)
                try:
                    plan_id = int(data.split(":", 1)[1])
                except ValueError:
                    plan_id = 0
                plan = db.session.get(Plan, plan_id)
                if not customer:
                    send_telegram_chat(chat_id, "⛔ Connect your Veyra account first.")
                elif not plan or not plan.active or not plan.product.active:
                    send_telegram_chat(chat_id, "❌ This plan is no longer available.")
                else:
                    o = create_telegram_order(customer, plan)
                    number = re.sub(r"\D", "", os.getenv("WHATSAPP_NUMBER", "+14242165211"))
                    wa_text = f"Hello, I want to complete Veyra order {public_order_ref(o)}. Product: {plan.product.name} - {plan.name}. Username: {customer.username}."
                    wa = f"https://wa.me/{number}?text={quote(wa_text)}" if number else None
                    kb = {"inline_keyboard": [[{"text": "💬 Complete order on WhatsApp", "url": wa}],[{"text": "🛍 More products", "callback_data": "cust_products"}]]} if wa else {"inline_keyboard": [[{"text": "🛍 More products", "callback_data": "cust_products"}]]}
                    price = plan.sale_price if plan.sale_price is not None else plan.price
                    send_telegram_chat(chat_id, f"✅ Order {public_order_ref(o)} created!\n\n📦 {plan.product.name} — {plan.name}\n💰 €{price:.2f}\n🟡 Status: Pending\n\nComplete your order through WhatsApp:", kb)
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "Order created"})
            return
        if data == "cust_products":
            with app.app_context():
                products=Product.query.filter_by(active=True).order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name).limit(30).all()
                lines=["🛍 Veyra Products", ""]
                for p in products:
                    plans=[x for x in p.plans if x.active]
                    if plans:
                        cheapest=min((x.sale_price if x.sale_price is not None else x.price) for x in plans)
                        lines.append(f"• {p.name} — from €{cheapest:.2f}")
                send_telegram_chat(chat_id, "\n".join(lines) if len(lines)>2 else "📭 No products are available right now.")
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if data == "cust_deals":
            with app.app_context():
                deals=Product.query.join(Plan).filter(Product.active==True, Plan.active==True, Plan.sale_price.isnot(None)).order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name).all()
                lines=["🔥 Veyra Deals", ""]
                seen=set()
                for p in deals:
                    if p.id in seen: continue
                    seen.add(p.id)
                    plans=[x for x in p.plans if x.active and x.sale_price is not None]
                    if plans:
                        lines.append(f"• {p.name} — from €{min(x.sale_price for x in plans):.2f}")
                send_telegram_chat(chat_id, "\n".join(lines) if len(lines)>2 else "📭 No active deals.")
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if data in {"cust_wishlist","cust_account","cust_support"}:
            with app.app_context():
                customer=customer_telegram_user(chat_id)
                if not customer:
                    send_telegram_chat(chat_id, "⛔ Connect your Veyra account first.")
                elif data == "cust_wishlist":
                    items=Wishlist.query.filter_by(user_id=customer.id).all()
                    send_telegram_chat(chat_id, "❤️ Your wishlist\n\n" + "\n".join(f"• {x.product.name}" for x in items) if items else "❤️ Your wishlist is empty.")
                elif data == "cust_account":
                    send_telegram_chat(chat_id, f"👤 Veyra account\nUsername: @{customer.username}\nEmail: {customer.email}\nTelegram: {customer_telegram_label(customer)}")
                else:
                    number=os.getenv("WHATSAPP_NUMBER", "+14242165211").strip()
                    send_telegram_chat(chat_id, f"💬 Veyra Support\n\nContact us on WhatsApp: https://wa.me/{re.sub(r'\D','',number)}" if number else "💬 Veyra Support\n\nPlease contact support through the Veyra website.")
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if not telegram_authorized(uid):
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "Not authorized.", "show_alert": True})
            return
        data = cq.get("data", "")
        chat_id = cq.get("message", {}).get("chat", {}).get("id", uid)
        if data == "cust_orders":
            with app.app_context():
                customer=customer_telegram_user(chat_id)
                kb=customer_orders_keyboard(customer.id) if customer else None
                send_telegram_chat(chat_id, "🛒 Your recent orders:", kb) if customer else send_telegram_chat(chat_id,"⛔ Account not linked.")
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if data == "cust_subs":
            with app.app_context():
                customer=customer_telegram_user(chat_id)
                send_telegram_chat(chat_id, customer_subscriptions_text(customer.id) if customer else "⛔ Account not linked.")
            telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            return
        if data == "admin_orders":
            kb = telegram_orders_keyboard()
            text = "📋 Pending / processing orders." if kb else "✅ No pending orders."
            send_telegram_chat(chat_id, text, kb)
        elif data.startswith("deliver:"):
            try: oid = int(data.split(":",1)[1])
            except ValueError: oid = 0
            with app.app_context():
                o = db.session.get(Order, oid)
                if not o:
                    send_telegram_chat(chat_id, "❌ Order not found.")
                elif o.status == "cancelled":
                    send_telegram_chat(chat_id, "❌ This order is cancelled.")
                else:
                    TELEGRAM_DELIVERY_DRAFTS[uid] = {"order_id": oid, "chat_id": chat_id, "stage": "content"}
                    send_telegram_chat(chat_id, telegram_order_text(o) + "\n\n✍️ Send the product/account details now.\nI will show you a preview before confirming delivery.")
        elif data == "delivery_confirm":
            draft = TELEGRAM_DELIVERY_DRAFTS.get(uid)
            if not draft or draft.get("stage") != "confirm":
                send_telegram_chat(chat_id, "⚠️ No delivery draft is waiting for confirmation.")
            else:
                with app.app_context():
                    o = db.session.get(Order, draft["order_id"])
                    if not o:
                        send_telegram_chat(chat_id, "❌ Order no longer exists.")
                    else:
                        existing = OrderDelivery.query.filter_by(order_id=o.id).first()
                        if existing:
                            send_telegram_chat(chat_id, "⚠️ This order already has a delivery recorded.")
                        else:
                            d = OrderDelivery(order_id=o.id, content=draft["content"], delivered_by_telegram_id=str(uid), sent_to_customer_telegram=False)
                            db.session.add(d)
                            previous = o.status
                            # Complete order and activate subscription/rewards exactly like website admin.
                            complete_order_record(o, source=f"telegram:{uid}")
                            db.session.commit()
                            sent = False
                            if o.user.telegram_id and str(o.user.telegram_id) != str(uid):
                                sent = send_telegram_chat(o.user.telegram_id, f"✅ Veyra delivery for order #{o.id}\n\n{draft['content']}\n\nYour order is now completed.")
                                d.sent_to_customer_telegram = sent
                                db.session.commit()
                            audit_note = f"{public_order_ref(o)} delivered via Telegram by {uid}; previous status {previous}"
                            app.logger.info(audit_note)
                            send_telegram_chat(chat_id, f"✅ Delivered and confirmed on website.\nOrder {public_order_ref(o)} is now COMPLETED." + ("\n📨 Sent to customer's Telegram." if sent else "\nℹ️ Customer Telegram is not linked, so delivery is stored on the website only."))
                            notify_admins(f"📦 Delivery confirmed from Telegram\nOrder {public_order_ref(o)}\n@{o.user.username}\n€{o.sale_price:.2f}")
                TELEGRAM_DELIVERY_DRAFTS.pop(uid, None)
        elif data == "delivery_cancel":
            TELEGRAM_DELIVERY_DRAFTS.pop(uid, None)
            send_telegram_chat(chat_id, "↩️ Delivery cancelled. Order remains unchanged.")
        telegram_api("answerCallbackQuery", {"callback_query_id": cq.get("id")})
        return

    msg = update.get("message") or {}
    tg_user = msg.get("from") or {}
    uid = tg_user.get("id")
    chat_id = msg.get("chat", {}).get("id", uid)
    text = (msg.get("text") or "").strip()
    if uid is not None:
        with app.app_context():
            linked = customer_telegram_user(chat_id)
            if linked:
                sync_telegram_profile(linked, tg_user, chat_id)
                db.session.commit()
    if not text:
        return
    if text.startswith("/"):
        command = text.split()[0].split("@",1)[0].lower()
        args = text.split()[1:]
        if command in {"/start", "/help"}:
            # Telegram deep-link: https://t.me/veyra2026_bot?start=link_TOKEN
            # This lets the Account page open Telegram directly and connect the account.
            if args and args[0].startswith("link_"):
                token = args[0][5:].strip()
                with app.app_context():
                    link=TelegramLink.query.filter_by(token=token, used=False).first()
                    if not link or link.expires_at < datetime.utcnow():
                        send_telegram_chat(chat_id, "❌ This Telegram link is invalid or has expired. Generate a new link from your Veyra account.")
                    elif User.query.filter(User.telegram_id==str(chat_id), User.id!=link.user_id).first():
                        send_telegram_chat(chat_id, "❌ This Telegram account is already linked to another Veyra account.")
                    else:
                        u=db.session.get(User, link.user_id)
                        u.telegram_id=str(chat_id); link.used=True
                        sync_telegram_profile(u, tg_user, chat_id)
                        db.session.commit()
                        telegram_api("setMyCommands", {"commands": [
                            {"command":"start","description":"Open Veyra"},
                            {"command":"products","description":"Browse products"},
                            {"command":"deals","description":"View deals"},
                            {"command":"orders","description":"My orders"},
                            {"command":"subscriptions","description":"My subscriptions"},
                            {"command":"wishlist","description":"My wishlist"},
                            {"command":"account","description":"My account"},
                            {"command":"support","description":"Contact support"}
                        ], "scope":{"type":"chat","chat_id":int(chat_id)}})
                        kb={"inline_keyboard":[[{"text":"🛍 Products","callback_data":"cust_products"}],[{"text":"🛒 My orders","callback_data":"cust_orders"},{"text":"🔐 Subscriptions","callback_data":"cust_subs"}],[{"text":"👤 My account","callback_data":"cust_account"},{"text":"💬 Support","callback_data":"cust_support"}]]}
                        send_telegram_chat(chat_id, f"✅ Telegram connected to @{u.username}.\n\n🛍 Welcome to Veyra! Choose a product to continue:", kb)
                        send_customer_products(chat_id)
                return
            if telegram_authorized(uid):
                kb={"inline_keyboard":[[{"text":"📋 Pending orders","callback_data":"admin_orders"}]]}
                send_telegram_chat(chat_id, "🤖 Veyra Admin\n\nUse the commands below or choose an action:", kb)
            else:
                customer=customer_telegram_user(chat_id)
                if customer:
                    kb={"inline_keyboard":[[{"text":"🛍 Products","callback_data":"cust_products"}],[{"text":"🔥 Deals","callback_data":"cust_deals"}],[{"text":"🛒 My orders","callback_data":"cust_orders"},{"text":"🔐 Subscriptions","callback_data":"cust_subs"}],[{"text":"❤️ Wishlist","callback_data":"cust_wishlist"}],[{"text":"👤 My account","callback_data":"cust_account"},{"text":"💬 Support","callback_data":"cust_support"}]]}
                    send_telegram_chat(chat_id, f"👋 Welcome back to Veyra, @{customer.username}!")
                    send_customer_products(chat_id)
                    send_telegram_chat(chat_id, "Use the menu below for your orders, subscriptions and account:", kb)
                else:
                    send_telegram_chat(chat_id, "👋 Welcome to Veyra!\n\nConnect your Veyra account from the Account page to unlock your orders, subscriptions and deliveries.")
            return
        if command == "/link":
            if not args:
                send_telegram_chat(chat_id, "Usage: /link CODE")
                return
            with app.app_context():
                link=TelegramLink.query.filter_by(token=args[0].strip(), used=False).first()
                if not link or link.expires_at < datetime.utcnow():
                    send_telegram_chat(chat_id, "❌ Link code is invalid or expired.")
                elif User.query.filter(User.telegram_id==str(chat_id), User.id!=link.user_id).first():
                    send_telegram_chat(chat_id, "❌ This Telegram account is already linked to another Veyra account.")
                else:
                    u=db.session.get(User, link.user_id)
                    u.telegram_id=str(chat_id); link.used=True
                    sync_telegram_profile(u, tg_user, chat_id)
                    db.session.commit()
                    telegram_api("setMyCommands", {"commands": [
                        {"command":"start","description":"Open Veyra"},
                        {"command":"products","description":"Browse products"},
                        {"command":"deals","description":"View deals"},
                        {"command":"orders","description":"My orders"},
                        {"command":"subscriptions","description":"My subscriptions"},
                        {"command":"wishlist","description":"My wishlist"},
                        {"command":"account","description":"My account"},
                        {"command":"support","description":"Contact support"}
                    ], "scope":{"type":"chat","chat_id":int(chat_id)}})
                    send_telegram_chat(chat_id, f"✅ Telegram connected to @{u.username}.\n\n🛍 Welcome to Veyra! Your products are ready below:")
                    send_customer_products(chat_id)
            return
        if command == "/orders" and not telegram_authorized(uid):
            with app.app_context():
                customer=customer_telegram_user(chat_id)
                kb=customer_orders_keyboard(customer.id) if customer else None
                send_telegram_chat(chat_id, "🛒 Your recent orders:", kb) if customer else send_telegram_chat(chat_id, "⛔ Connect your Veyra account first with /link CODE")
            return
        if command == "/subscriptions" and not telegram_authorized(uid):
            with app.app_context():
                customer=customer_telegram_user(chat_id)
                send_telegram_chat(chat_id, customer_subscriptions_text(customer.id) if customer else "⛔ Connect your Veyra account first with /link CODE")
            return
        if command in {"/admin", "/deliver"} and telegram_authorized(uid):
            kb = telegram_orders_keyboard()
            send_telegram_chat(chat_id, "📋 Zgjidh porosinë që dëshiron ta dorëzosh:" if kb else "✅ Nuk ka porosi në pritje.", kb)
            return
        if command == "/orders" and telegram_authorized(uid):
            kb = telegram_orders_keyboard()
            send_telegram_chat(chat_id, "📋 Zgjidh porosinë që dëshiron ta dorëzosh:" if kb else "✅ Nuk ka porosi në pritje.", kb)
            return
        if command == "/settelegram" and telegram_authorized(uid):
            if len(args) != 2:
                send_telegram_chat(chat_id, "Usage: /settelegram username telegram_id")
                return
            username, target_id = args[0].lstrip("@"), args[1].strip()
            if not target_id.lstrip("-").isdigit():
                send_telegram_chat(chat_id, "Telegram ID must be numeric.")
                return
            with app.app_context():
                customer = User.query.filter_by(username=username, is_admin=False).first()
                if not customer:
                    send_telegram_chat(chat_id, f"❌ Customer @{username} not found.")
                elif User.query.filter(User.telegram_id == target_id, User.id != customer.id).first():
                    send_telegram_chat(chat_id, "❌ That Telegram ID is already linked to another account.")
                else:
                    customer.telegram_id = target_id
                    profile = TelegramProfile.query.filter_by(user_id=customer.id).first()
                    if not profile:
                        profile = TelegramProfile(user_id=customer.id, telegram_id=target_id)
                        db.session.add(profile)
                    else:
                        profile.telegram_id = target_id
                    db.session.commit()
                    send_telegram_chat(chat_id, f"✅ Telegram linked to @{customer.username}. Future confirmed deliveries can be sent directly to this Telegram account.")
            return

        if command == "/deals" and not telegram_authorized(uid):
            with app.app_context():
                deals = Product.query.join(Plan).filter(Product.active==True, Plan.active==True, Plan.sale_price.isnot(None)).order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name).all()
                lines=["🔥 Veyra deals", ""]
                seen=set()
                for p in deals:
                    if p.id in seen: continue
                    seen.add(p.id)
                    plans=[x for x in p.plans if x.active and x.sale_price is not None]
                    if plans:
                        lines.append(f"• {p.name} — from €{min(x.sale_price for x in plans):.2f}")
                send_telegram_chat(chat_id, "\n".join(lines) if len(lines)>2 else "📭 No active deals.")
            return
        if command == "/wishlist" and not telegram_authorized(uid):
            with app.app_context():
                customer=customer_telegram_user(chat_id)
                if not customer:
                    send_telegram_chat(chat_id, "⛔ Connect your Veyra account first with /link CODE")
                else:
                    items=Wishlist.query.filter_by(user_id=customer.id).all()
                    send_telegram_chat(chat_id, "❤️ Your wishlist\n\n" + "\n".join(f"• {x.product.name}" for x in items) if items else "❤️ Your wishlist is empty.")
            return
        if command == "/account" and not telegram_authorized(uid):
            with app.app_context():
                customer=customer_telegram_user(chat_id)
                if not customer:
                    send_telegram_chat(chat_id, "⛔ Connect your Veyra account first with /link CODE")
                else:
                    send_telegram_chat(chat_id, f"👤 Veyra account\nUsername: @{customer.username}\nEmail: {customer.email}\nTelegram: {customer_telegram_label(customer)}")
            return
        if command == "/support" and not telegram_authorized(uid):
            number=os.getenv("WHATSAPP_NUMBER", "+14242165211").strip()
            if number:
                send_telegram_chat(chat_id, f"💬 Veyra Support\n\nContact us on WhatsApp: https://wa.me/{re.sub(r'\D','',number)}")
            else:
                send_telegram_chat(chat_id, "💬 Veyra Support\n\nPlease contact support through the Veyra website.")
            return

        if command == "/products" and not telegram_authorized(uid):
            with app.app_context():
                if not customer_telegram_user(chat_id):
                    send_telegram_chat(chat_id, "⛔ Connect your Veyra account first with /link CODE")
                else:
                    send_customer_products(chat_id)
            return
        if command == "/products" and telegram_authorized(uid):
            with app.app_context():
                products=Product.query.filter_by(active=True).order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name).limit(30).all()
                text="🛍 Active products\n\n"+"\n".join(f"• {p.name} — {len([x for x in p.plans if x.active])} active plans" for p in products) if products else "📭 No active products."
                send_telegram_chat(chat_id,text)
            return
        if command == "/customers" and telegram_authorized(uid):
            with app.app_context():
                total=User.query.filter_by(is_admin=False).count(); linked=User.query.filter(User.is_admin==False, User.telegram_id.isnot(None)).count()
                send_telegram_chat(chat_id,f"👥 Customers\nTotal: {total}\nTelegram linked: {linked}")
            return
        if command == "/subscriptions" and telegram_authorized(uid):
            with app.app_context():
                active=Subscription.query.filter_by(active=True).count()
                send_telegram_chat(chat_id,f"🔐 Active subscriptions: {active}")
            return
        if command == "/stats" and telegram_authorized(uid):
            with app.app_context():
                orders = Order.query.all()
                revenue = sum(o.sale_price for o in orders if o.status != "cancelled")
                profit = sum(o.profit for o in orders if o.status != "cancelled")
                send_telegram_chat(chat_id, f"📊 Veyra Stats\nOrders: {len(orders)}\nSales: €{revenue:.2f}\nProfit: €{profit:.2f}")
            return
        if command == "/cancel" and telegram_authorized(uid):
            TELEGRAM_DELIVERY_DRAFTS.pop(uid, None)
            send_telegram_chat(chat_id, "↩️ Draft cancelled.")
            return
        if command == "/link":
            send_telegram_chat(chat_id, "Për lidhjen e klientit me Telegram përdor kodin e lidhjes nga llogaria Veyra.")
            return
        if not telegram_authorized(uid):
            send_telegram_chat(chat_id, "⛔ Admin access denied.")
            return

    # Content entered after clicking Deliver.
    draft = TELEGRAM_DELIVERY_DRAFTS.get(uid)
    if draft and draft.get("stage") == "content":
        content = text[:10000]
        draft["content"] = content
        draft["stage"] = "confirm"
        kb = {"inline_keyboard": [[
            {"text": "✅ Confirm & deliver", "callback_data": "delivery_confirm"},
            {"text": "✖ Cancel", "callback_data": "delivery_cancel"}
        ]]}
        send_telegram_chat(chat_id, "🔎 DELIVERY PREVIEW\n\n" + content + "\n\nConfirm to save it on the website and complete the order.", kb)

TELEGRAM_LAST_UPDATE = 0
TELEGRAM_THREAD = None

def telegram_poll_loop():
    global TELEGRAM_LAST_UPDATE
    while True:
        try:
            if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or len(telegram_admin_ids()) != 2:
                time.sleep(10)
                continue
            result = telegram_api("getUpdates", {"offset": TELEGRAM_LAST_UPDATE + 1, "timeout": 20, "allowed_updates": ["message", "callback_query"]})
            if result and result.get("ok"):
                for upd in result.get("result", []):
                    TELEGRAM_LAST_UPDATE = max(TELEGRAM_LAST_UPDATE, upd.get("update_id", 0))
                    try: telegram_handle_update(upd)
                    except Exception as exc: app.logger.exception("Telegram update failed: %s", exc)
        except Exception as exc:
            app.logger.warning("Telegram poll loop: %s", exc)
        time.sleep(1)

def start_telegram_bot():
    global TELEGRAM_THREAD
    if TELEGRAM_THREAD and TELEGRAM_THREAD.is_alive():
        return
    if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
        return
    # Avoid a second polling thread under Flask's development reloader.
    if app.debug and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return
    TELEGRAM_THREAD = threading.Thread(target=telegram_poll_loop, name="veyra-telegram", daemon=True)
    TELEGRAM_THREAD.start()



with app.app_context():
    try:
        db.create_all()
        # Lightweight compatibility migration for existing Railway databases.
        cols = {c["name"] for c in inspect(db.engine).get_columns("product")}
        if "price_label" not in cols:
            db.session.execute(db.text("ALTER TABLE product ADD COLUMN price_label VARCHAR(60) DEFAULT 'month'"))
        if "sort_order" not in cols:
            db.session.execute(db.text("ALTER TABLE product ADD COLUMN sort_order INTEGER DEFAULT 0"))
        if "card_label" not in cols:
            db.session.execute(db.text("ALTER TABLE product ADD COLUMN card_label VARCHAR(140) DEFAULT ''"))
        plan_cols = {c["name"] for c in inspect(db.engine).get_columns("plan")}
        if "sort_order" not in plan_cols:
            db.session.execute(db.text("ALTER TABLE plan ADD COLUMN sort_order INTEGER DEFAULT 0"))
        db.session.commit()
        configure_telegram_commands()
    except Exception as exc:
        app.logger.warning("Telegram command setup skipped: %s", exc)

def notify_admins(message, event="new_order", order=None):
    app.logger.info("ADMIN NOTIFICATION [%s]: %s", event, message)
    row = AdminSetting.query.filter_by(key=f"notify_{event}").first() if has_app_context() else None
    enabled = True if not row or row.value == "" else row.value == "on"
    if not enabled:
        return
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        kb = None
        if event == "new_order" and order is not None:
            number = re.sub(r"\D", "", os.getenv("WHATSAPP_NUMBER", "+14242165211"))
            item = f"{order.plan.product.name} — {order.plan.name}" if order.plan else (order.bundle.name if order.bundle else "Order")
            wa_text = f"Hello, I want to complete Veyra order {public_order_ref(order)}. Product: {item}. Customer: @{order.user.username}."
            wa = f"https://wa.me/{number}?text={quote(wa_text)}" if number else None
            rows = [[{"text": "📦 Deliver", "callback_data": f"deliver:{order.id}"}]]
            if wa:
                rows.append([{"text": "💬 Open WhatsApp", "url": wa}])
            kb = {"inline_keyboard": rows}
        send_telegram(message, kb)
    # WhatsApp is intentionally optional; it requires Meta Cloud API credentials
    # and an approved template. Send only new-order notifications by default.
    if event == "new_order":
        send_whatsapp_admin_notification(message)

def audit(action, details=""):
    if current_user.is_authenticated and current_user.is_admin:
        db.session.add(AuditLog(admin_user_id=current_user.id, action=action, details=details))

@app.route("/")
def index():
    # Always use the current Veyra storefront here. Keeping this explicit avoids
    # relying on a late view-function replacement and guarantees bundles/deals
    # use the same storefront template in production.
    products = Product.query.filter_by(active=True).order_by(Product.sort_order.asc(), Product.id).all()
    categories = Category.query.order_by(Category.name).all()
    bundles = Bundle.query.filter_by(active=True).order_by(Bundle.featured.desc(), Bundle.name).all()
    deals = [d for d in V21Deal.query.order_by(V21Deal.created_at.desc()).all() if v21_deal_active(d)] if "V21Deal" in globals() else []
    recent = v21_recent() if "v21_recent" in globals() else []
    return render_template("v21_index.html", products=products, categories=categories, deals=deals, bundles=bundles, recent=recent)

@app.route("/categories")
def categories():
    categories = Category.query.order_by(Category.name).all()
    products = Product.query.filter_by(active=True).order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name).all()
    return render_template("categories.html", categories=categories, products=products)

@app.route("/ref/<code>")
def referral_landing(code):
    if Referral.query.filter_by(code=code).first():
        session["pending_referral"] = code
        flash("Referral saved. The reward is activated only after your first completed purchase.")
    return redirect(url_for("register"))

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if not username or not email or not password:
            flash("Plotëso të gjitha fushat.")
        elif User.query.filter_by(username=username).first():
            flash("Ky username ekziston.")
        elif User.query.filter_by(email=email).first():
            flash("Ky email ekziston.")
        else:
            u = User(username=username, email=email, password_hash=generate_password_hash(password))
            db.session.add(u); db.session.flush()
            ref_code = session.get("pending_referral")
            if ref_code:
                ref = Referral.query.filter_by(code=ref_code).first()
                if ref and ref.referrer_id != u.id and ref.referred_id is None:
                    ref.referred_id = u.id
                    db.session.add(ref)
            # Every customer receives a permanent referral code, but no points are granted for registration.
            code = f"{username.lower().replace(' ','-')}-{u.id}"
            if not Referral.query.filter_by(code=code).first():
                db.session.add(Referral(referrer_id=u.id, code=code))
            db.session.commit()
            session.pop("pending_referral", None)
            login_user(u)
            return redirect(url_for("account"))
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(username=request.form["username"].strip()).first()
        if u and check_password_hash(u.password_hash, request.form["password"]):
            session.clear()
            session.permanent = True
            login_user(u)
            csrf_token()
            return redirect(url_for("admin" if u.is_admin else "account"))
        flash("Username ose password gabim.")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("index"))

@app.route("/account/telegram-link", methods=["POST"])
@login_required
def account_telegram_link():
    if current_user.is_admin:
        flash("Admin accounts are linked through the Admin Telegram settings.")
        return redirect(url_for("account"))
    TelegramLink.query.filter_by(user_id=current_user.id, used=False).delete(synchronize_session=False)
    token=secrets.token_urlsafe(10)
    db.session.add(TelegramLink(user_id=current_user.id, token=token, expires_at=datetime.utcnow()+timedelta(minutes=15)))
    db.session.commit()
    bot_username=os.getenv("TELEGRAM_BOT_USERNAME", "veyra2026_bot").lstrip("@").strip()
    telegram_url=f"https://t.me/{bot_username}?start=link_{token}"
    flash(f"Telegram link ready: {telegram_url} — or send /link {token} to @{bot_username} within 15 minutes.")
    return redirect(url_for("account"))

@app.route("/account")
@login_required
def account():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    subs = Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.expires_at.desc()).all()
    points = db.session.query(db.func.coalesce(db.func.sum(LoyaltyPoint.points),0)).filter(LoyaltyPoint.user_id==current_user.id).scalar() or 0
    point_history = LoyaltyPoint.query.filter_by(user_id=current_user.id).order_by(LoyaltyPoint.created_at.desc()).limit(20).all()
    wishlist_items = Wishlist.query.filter_by(user_id=current_user.id).all()
    my_ref = Referral.query.filter_by(referrer_id=current_user.id, referred_id=None).first()
    if not my_ref:
        my_ref = Referral.query.filter_by(referrer_id=current_user.id).order_by(Referral.id.desc()).first()
    telegram_link = TelegramLink.query.filter_by(user_id=current_user.id, used=False).order_by(TelegramLink.id.desc()).first()
    telegram_url = None
    if telegram_link and telegram_link.expires_at >= datetime.utcnow():
        bot_username=os.getenv("TELEGRAM_BOT_USERNAME", "veyra2026_bot").lstrip("@").strip()
        telegram_url=f"https://t.me/{bot_username}?start=link_{telegram_link.token}"
    else:
        telegram_link = None
    return render_template("account.html", orders=orders, subs=subs, points=points, referral=my_ref, point_history=point_history, wishlist_items=wishlist_items, telegram_link=telegram_link, telegram_url=telegram_url)

@app.route("/product/<slug>")
def product(slug):
    p = Product.query.filter_by(slug=slug, active=True).first_or_404()
    reviews = Review.query.filter_by(product_id=p.id, approved=True).order_by(Review.created_at.desc()).all()
    return render_template("product.html", product=p, reviews=reviews)

@app.route("/order/<int:plan_id>", methods=["POST"])
@login_required
def order(plan_id):
    plan = db.session.get(Plan, plan_id)
    if not plan or not plan.active or not plan.product.active:
        flash("Plani nuk është i disponueshëm.")
        return redirect(url_for("index"))
    selling_price = plan.sale_price if plan.sale_price is not None else plan.price
    discount, coupon, coupon_error = apply_coupon(request.form.get("coupon"), selling_price, current_user.id, plan.id)
    if request.form.get("coupon") and coupon_error:
        flash(coupon_error)
        return redirect(url_for("product", slug=plan.product.slug))
    final_price = round(selling_price - discount, 2)
    cost = plan.cost_price or 0.0
    o = Order(
        user_id=current_user.id,
        plan_id=plan.id,
        sale_price=final_price,
        cost_price=cost,
        profit=final_price - cost,
        discount=discount,
        coupon_code=coupon.code if coupon else None
    )
    if coupon:
        coupon.used_count += 1
        db.session.add(coupon)
        db.session.add(CouponUsage(coupon_id=coupon.id, user_id=current_user.id, order_id=None))
    db.session.add(o); db.session.flush()
    if coupon:
        usage = CouponUsage.query.filter_by(coupon_id=coupon.id, user_id=current_user.id).first()
        if usage: usage.order_id = o.id
    audit("New order", f"{public_order_ref(o)} — {plan.product.name} / {plan.name} — €{final_price:.2f}"); db.session.commit()
    notify_admins(customer_order_notification(o), "new_order", o)
    number = os.getenv("WHATSAPP_NUMBER","" )
    msg = f"Hello, I want to order {plan.product.name} - {plan.name}. Order {public_order_ref(o)}. Username: {current_user.username}"
    wa = f"https://wa.me/{number}?text={quote(msg)}" if number else "#"
    return render_template("order.html", order=o, wa=wa)

@app.route("/payment/<int:order_id>", methods=["POST"])
@login_required
def start_payment(order_id):
    o = db.session.get(Order, order_id)
    if not o or o.user_id != current_user.id:
        return ("Not found", 404)
    provider = AdminSetting.query.filter_by(key="payment_provider").first()
    if not provider or provider.value != "stripe":
        return redirect(url_for("order_payment_unavailable", order_id=o.id))
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        flash("Stripe is selected but STRIPE_SECRET_KEY is not configured.")
        return redirect(url_for("order_payment_unavailable", order_id=o.id))
    payload = {
        "mode":"payment", "success_url":request.host_url.rstrip("/")+url_for("payment_success")+"?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url":request.host_url.rstrip("/")+url_for("payment_cancel", order_id=o.id),
        "client_reference_id":str(o.id), "metadata[order_id]":str(o.id),
        "line_items[0][price_data][currency]":"eur",
        "line_items[0][price_data][product_data][name]":(o.plan.product.name if o.plan else o.bundle.name),
        "line_items[0][price_data][unit_amount]":str(int(round(o.sale_price*100))),
        "line_items[0][quantity]":"1"}
    try:
        r=requests.post("https://api.stripe.com/v1/checkout/sessions",data=payload,auth=(secret,""),timeout=20)
        data=r.json()
        if not r.ok or not data.get("url"):
            app.logger.warning("Stripe checkout error: %s", data)
            flash("Unable to start Stripe checkout.")
            return redirect(url_for("order_payment_unavailable", order_id=o.id))
        audit("Stripe checkout started", f"{public_order_ref(o)}")
        db.session.commit()
        return redirect(data["url"])
    except Exception as exc:
        app.logger.warning("Stripe checkout exception: %s", exc)
        flash("Payment service is temporarily unavailable.")
        return redirect(url_for("order_payment_unavailable", order_id=o.id))

@app.route("/payment/unavailable/<int:order_id>")
@login_required
def order_payment_unavailable(order_id):
    o=db.session.get(Order,order_id)
    if not o or o.user_id != current_user.id: return ("Not found",404)
    return render_template("order.html", order=o, wa="#", payment_unavailable=True)

@app.route("/payment/success")
@login_required
def payment_success():
    sid=request.args.get("session_id","").strip(); secret=os.getenv("STRIPE_SECRET_KEY","").strip()
    if not sid or not secret: return ("Invalid payment session",400)
    try:
        r=requests.get(f"https://api.stripe.com/v1/checkout/sessions/{quote(sid,safe='')}",auth=(secret,""),timeout=20); data=r.json()
    except Exception: return ("Payment verification failed",502)
    try: oid=int(data.get("metadata",{}).get("order_id") or data.get("client_reference_id") or 0)
    except ValueError: oid=0
    o=db.session.get(Order,oid)
    if not o or o.user_id != current_user.id: return ("Payment order not found",404)
    if data.get("payment_status") == "paid":
        o.status="processing"; audit("Payment verified",f"{public_order_ref(o)} paid via Stripe"); db.session.commit()
        notify_admins(f"💳 Payment confirmed for order {public_order_ref(o)}\n@{o.user.username}\n€{o.sale_price:.2f}", "payment")
        flash("Payment confirmed. Your order is now being processed.")
    return redirect(url_for("account"))

@app.route("/payment/cancel/<int:order_id>")
@login_required
def payment_cancel(order_id):
    o=db.session.get(Order,order_id)
    if not o or o.user_id != current_user.id: return ("Not found",404)
    flash("Payment was cancelled. Your order remains pending.")
    return redirect(url_for("account"))

@app.route("/bundle/<int:bundle_id>/order", methods=["POST"])
@login_required
def bundle_order(bundle_id):
    bundle = db.session.get(Bundle, bundle_id)
    if not bundle or not bundle.active or not bundle.items:
        flash("Oferta nuk është e disponueshme.")
        return redirect(url_for("index"))
    cost = sum((item.plan.cost_price or 0.0) * item.quantity for item in bundle.items)
    discount, coupon, coupon_error = apply_coupon(request.form.get("coupon"), bundle.price, current_user.id)
    if request.form.get("coupon") and coupon_error:
        flash(coupon_error)
        return redirect(url_for("index") + "#bundles")
    final_price = round(bundle.price - discount, 2)
    o = Order(user_id=current_user.id, bundle_id=bundle.id,
              sale_price=final_price, cost_price=cost,
              profit=final_price - cost, discount=discount,
              coupon_code=coupon.code if coupon else None)
    if coupon:
        coupon.used_count += 1
        db.session.add(coupon)
        db.session.add(CouponUsage(coupon_id=coupon.id, user_id=current_user.id, order_id=None))
    db.session.add(o); db.session.flush()
    if coupon:
        usage = CouponUsage.query.filter_by(coupon_id=coupon.id, user_id=current_user.id).first()
        if usage: usage.order_id = o.id
    audit("New bundle order", f"{public_order_ref(o)} — {bundle.name} — €{final_price:.2f}"); db.session.commit()
    notify_admins(customer_order_notification(o), "new_order", o)
    number = os.getenv("WHATSAPP_NUMBER","" )
    item_names = " + ".join(f"{i.plan.product.name} ({i.plan.name})" for i in bundle.items)
    msg = f"Hello, I want to order bundle: {bundle.name}. Items: {item_names}. Order {public_order_ref(o)}. Username: {current_user.username}"
    wa = f"https://wa.me/{number}?text={quote(msg)}" if number else "#"
    return render_template("order.html", order=o, wa=wa)


@app.route("/wishlist/<int:product_id>", methods=["POST"])
@login_required
def wishlist(product_id):
    p = db.session.get(Product, product_id)
    if not p:
        return ("Not found", 404)
    existing = Wishlist.query.filter_by(user_id=current_user.id, product_id=p.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(Wishlist(user_id=current_user.id, product_id=p.id))
    db.session.commit()
    return redirect(request.referrer or url_for("index"))

@app.route("/wishlist")
@login_required
def wishlist_page():
    items = Wishlist.query.filter_by(user_id=current_user.id).order_by(Wishlist.id.desc()).all()
    return render_template("wishlist.html", items=items)

@app.route("/review/<int:product_id>", methods=["POST"])
@login_required
def review(product_id):
    p = db.session.get(Product, product_id)
    if not p:
        return ("Not found",404)
    has_order = Order.query.join(Plan, Order.plan_id == Plan.id, isouter=True).filter(
        Order.user_id == current_user.id,
        db.or_(Plan.product_id == product_id, Order.bundle_id.isnot(None)),
        Order.status == "completed"
    ).first()
    if not has_order:
        flash("You can review a product after a completed order.")
        return redirect(request.referrer or url_for("product", slug=p.slug))
    existing_review = Review.query.filter_by(user_id=current_user.id, product_id=product_id).first()
    if existing_review:
        flash("You have already reviewed this product.")
        return redirect(request.referrer or url_for("product", slug=p.slug))
    rating = max(1, min(5, int(request.form.get("rating",5))))
    db.session.add(Review(user_id=current_user.id, product_id=product_id, rating=rating,
                          text=request.form.get("text","").strip(), approved=False))
    db.session.commit()
    flash("Review submitted for approval.")
    return redirect(request.referrer or url_for("product", slug=p.slug))

@app.route("/admin/review/<int:review_id>/<action>", methods=["POST"])
@login_required
def review_action(review_id, action):
    if not admin_required(): return ("Forbidden",403)
    r = db.session.get(Review, review_id)
    if not r: return ("Not found",404)
    if action == "approve": r.approved = True
    elif action == "reject": db.session.delete(r)
    else: return ("Bad action",400)
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/admin")
@login_required
def admin():
    if not admin_required(): return ("Forbidden",403)
    orders = Order.query.order_by(Order.created_at.desc()).all()
    revenue = sum(o.sale_price or 0 for o in orders if o.status != "cancelled")
    costs = sum(o.cost_price or 0 for o in orders if o.status != "cancelled")
    profit = sum(o.profit or 0 for o in orders if o.status != "cancelled")
    users = User.query.filter_by(is_admin=False).all()
    reviews = Review.query.order_by(Review.created_at.desc()).all()
    coupons = Coupon.query.order_by(Coupon.id.desc()).all()
    wishlist_count = Wishlist.query.count()
    points_total = db.session.query(db.func.coalesce(db.func.sum(LoyaltyPoint.points),0)).scalar() or 0
    # Phase 6 analytics: compact, database-backed reporting for the last 14 days.
    today = datetime.utcnow().date()
    analytics_days = []
    for offset in range(29, -1, -1):
        day = today - timedelta(days=offset)
        day_orders = [o for o in orders if o.created_at and o.created_at.date() == day and o.status != "cancelled"]
        analytics_days.append({
            "label": day.strftime("%d.%m"),
            "sales": round(sum(o.sale_price or 0 for o in day_orders), 2),
            "profit": round(sum(o.profit or 0 for o in day_orders), 2),
            "orders": len(day_orders),
        })
    product_sales = {}
    for o in orders:
        if o.status == "cancelled":
            continue
        name = o.plan.product.name if o.plan and o.plan.product else (o.bundle.name if o.bundle else "Unknown")
        product_sales[name] = product_sales.get(name, 0) + (o.sale_price or 0)
    top_products = sorted(product_sales.items(), key=lambda x: x[1], reverse=True)[:8]
    avg_order = round(revenue / len([o for o in orders if o.status != "cancelled"]), 2) if any(o.status != "cancelled" for o in orders) else 0
    return render_template("admin.html",
        products=Product.query.order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name).all(), bundles=Bundle.query.order_by(Bundle.featured.desc(), Bundle.name).all(),
        plans=Plan.query.join(Product).order_by(Product.sort_order.asc(), Product.featured.desc(), Product.name, Plan.sort_order.asc(), Plan.months, Plan.id).all(), categories=Category.query.order_by(Category.name).all(),
        orders=orders, users=users,
        subscriptions=Subscription.query.order_by(Subscription.expires_at.desc()).all(),
        reviews=reviews, coupons=coupons, wishlist_count=wishlist_count, points_total=points_total,
        revenue=revenue, costs=costs, profit=profit, avg_order=avg_order,
        analytics_days=analytics_days, top_products=top_products,
        expiring_subscriptions=Subscription.query.filter(Subscription.active==True, Subscription.expires_at <= datetime.utcnow()+timedelta(days=7)).order_by(Subscription.expires_at.asc()).limit(50).all(),
        active_products=Product.query.filter_by(active=True).count(),
        active_subscriptions=Subscription.query.filter_by(active=True).count(),
        pending_orders=Order.query.filter_by(status="pending").count(),
        telegram_ids=telegram_admin_ids(),
        telegram_ready=bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        audit_logs=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(30).all(),
        store_settings={x.key:x.value for x in AdminSetting.query.all()},
        new_ticket_count=V21Ticket.query.filter_by(status="new").count() if "V21Ticket" in globals() else 0)

@app.route("/admin/plan/<int:plan_id>", methods=["POST"])
@login_required
def edit_plan(plan_id):
    if not admin_required(): return ("Forbidden",403)
    try:
        v21_save_catalog_backup("Before catalog change", "auto")
    except Exception:
        pass
    p = db.session.get(Plan, plan_id)
    p.price = float(request.form["price"])
    sale = request.form.get("sale_price","").strip()
    cost = request.form.get("cost_price","").strip()
    p.sale_price = float(sale) if sale else None
    p.cost_price = float(cost) if cost else 0.0
    db.session.commit()
    flash("Çmimi u ruajt.")
    return redirect(url_for("admin"))

def complete_order_record(o, source="admin"):
    """Complete an order once, activate subscriptions and award referral points."""
    previous = o.status
    if previous == "completed":
        return False
    o.status = "completed"
    start = datetime.utcnow()
    plans_to_activate = []
    if o.plan_id and o.plan:
        plans_to_activate.append(o.plan)
    elif o.bundle_id and o.bundle:
        plans_to_activate.extend([item.plan for item in o.bundle.items if item.plan])
    for plan in plans_to_activate:
        existing = Subscription.query.filter_by(user_id=o.user_id, product_id=plan.product_id, active=True).order_by(Subscription.expires_at.desc()).first()
        sub_start = max(start, existing.expires_at) if existing else start
        expires = sub_start + timedelta(days=30 * max(1, plan.months))
        if existing and existing.expires_at > start:
            existing.expires_at = expires
            existing.plan_id = plan.id
            existing.order_id = o.id
        else:
            db.session.add(Subscription(user_id=o.user_id, product_id=plan.product_id, started_at=sub_start, expires_at=expires, active=True, plan_id=plan.id, order_id=o.id))
    # Loyalty: award configurable points on each completed order.
    points_per_eur = loyalty_setting("loyalty_points_per_eur", 1)
    earned = int(max(0, round((o.sale_price or 0) * points_per_eur)))
    if earned:
        db.session.add(LoyaltyPoint(user_id=o.user_id, points=earned, reason=f"Purchase reward — order {public_order_ref(o)}"))

    ref = Referral.query.filter_by(referred_id=o.user_id, rewarded=False).first()
    if ref and ref.referrer_id != o.user_id:
        reward = int(loyalty_setting("referral_reward_points", os.getenv("REFERRAL_REWARD_POINTS", "100")))
        db.session.add(LoyaltyPoint(user_id=ref.referrer_id, points=reward, reason=f"Referral reward — order {public_order_ref(o)}"))
        db.session.add(LoyaltyPoint(user_id=o.user_id, points=reward, reason=f"Referral reward — first purchase {public_order_ref(o)}"))
        ref.reward_points = reward
        ref.rewarded = True
    return True


@app.route("/admin/order/manual", methods=["POST"])
@login_required
def admin_manual_order():
    if not admin_required(): return ("Forbidden", 403)
    try:
        user_id_raw = request.form.get("user_id", "").strip()
        customer_label = request.form.get("customer_name", "").strip().lstrip("@").strip()
        plan_id_raw = request.form.get("plan_id", "").strip()
        bundle_id_raw = request.form.get("bundle_id", "").strip()
        status = request.form.get("status", "pending").strip().lower()
        if status not in {"pending", "processing", "completed"}:
            status = "pending"

        user = None
        # Existing customer selection is optional. If no registered customer exists,
        # the admin can type a name/username and Veyra creates a lightweight manual
        # customer record automatically so the order still has a stable owner.
        if user_id_raw:
            try:
                user = db.session.get(User, int(user_id_raw))
            except (TypeError, ValueError):
                user = None
            if not user or user.is_admin:
                user = None

        if customer_label:
            # Prefer an exact username match before creating a new manual client.
            existing = User.query.filter(db.func.lower(User.username) == customer_label.lower()).first()
            if existing and not existing.is_admin:
                user = existing
            elif not user:
                base = re.sub(r"[^A-Za-z0-9_.-]+", "_", customer_label).strip("_.-")[:32] or "customer"
                username = base
                n = 2
                while User.query.filter_by(username=username).first():
                    suffix = f"_{n}"
                    username = (base[:40-len(suffix)] + suffix)
                    n += 1
                email = f"manual.{secrets.token_hex(8)}@customer.veyra.local"
                user = User(username=username, email=email,
                            password_hash=generate_password_hash(secrets.token_urlsafe(24)))
                db.session.add(user)
                db.session.flush()

        if not user or user.is_admin:
            flash("Choose a customer or write a customer name/username.")
            return redirect(url_for("admin") + "#orders")

        plan = db.session.get(Plan, int(plan_id_raw)) if plan_id_raw else None
        bundle = db.session.get(Bundle, int(bundle_id_raw)) if bundle_id_raw else None
        if (plan is None) == (bundle is None):
            flash("Choose exactly one product plan or bundle.")
            return redirect(url_for("admin") + "#orders")
        if plan and (not plan.active or not plan.product or not plan.product.active):
            flash("That plan is not active.")
            return redirect(url_for("admin") + "#orders")
        if bundle and (not bundle.active or not bundle.items):
            flash("That bundle is not active or has no plans.")
            return redirect(url_for("admin") + "#orders")

        if plan:
            default_price = plan.sale_price if plan.sale_price is not None else plan.price
            default_cost = plan.cost_price or 0.0
        else:
            default_price = bundle.price
            default_cost = sum((x.plan.cost_price or 0.0) * x.quantity for x in bundle.items if x.plan)

        sale_raw = request.form.get("sale_price", "").strip()
        sale_price = float(sale_raw) if sale_raw else float(default_price)
        cost_price = float(default_cost)
        o = Order(user_id=user.id, plan_id=plan.id if plan else None, bundle_id=bundle.id if bundle else None,
                  status="pending", sale_price=sale_price, cost_price=cost_price,
                  profit=round(sale_price-cost_price, 2), discount=0.0)
        db.session.add(o)
        db.session.flush()
        if status == "processing":
            o.status = "processing"
        db.session.commit()
        if status == "completed":
            complete_order_record(o, source="admin-manual")
            db.session.commit()
        audit("Manual order created", f"{public_order_ref(o)} — @{user.username}")
        db.session.commit()
        notify_admins(customer_order_notification(o), "new_order", o)
        flash(f"Manual order {public_order_ref(o)} created for @{user.username}.")
    except Exception as exc:
        db.session.rollback()
        app.logger.exception("Manual order creation failed")
        flash(f"Could not create manual order: {exc}")
    return redirect(url_for("admin") + "#orders")

@app.route("/admin/order/<int:order_id>")
@login_required
def admin_order_detail(order_id):
    if not admin_required(): return ("Forbidden",403)
    o=db.session.get(Order, order_id)
    if not o: return ("Not found",404)
    return render_template("admin_order.html", order=o)

@app.route("/admin/order/<int:order_id>/deliver", methods=["POST"])
@login_required
def admin_order_deliver(order_id):
    if not admin_required(): return ("Forbidden",403)
    o=db.session.get(Order, order_id)
    if not o: return ("Not found",404)
    content=request.form.get("content","").strip()[:10000]
    if not content:
        flash("Delivery content is required.")
        return redirect(url_for("admin_order_detail", order_id=order_id))
    existing=OrderDelivery.query.filter_by(order_id=o.id).first()
    if existing:
        existing.content=content
        existing.delivered_at=datetime.utcnow()
        existing.delivered_by_telegram_id=None
        existing.sent_to_customer_telegram=False
    else:
        db.session.add(OrderDelivery(order_id=o.id, content=content, delivered_by_telegram_id=None, sent_to_customer_telegram=False))
    previous=o.status
    complete_order_record(o, source=f"web:{current_user.username}")
    audit("Order delivered", f"{public_order_ref(o)}: {previous} → completed")
    db.session.commit()
    flash("Delivery saved and order completed.")
    if o.user.telegram_id:
        sent=send_telegram_chat(o.user.telegram_id, f"✅ Veyra delivery for order #{o.id}\n\n{content}\n\nYour order is now completed.")
        d=OrderDelivery.query.filter_by(order_id=o.id).first(); d.sent_to_customer_telegram=sent; db.session.commit()
    return redirect(url_for("admin_order_detail", order_id=order_id))

@app.route("/admin/order/<int:order_id>/resend-telegram", methods=["POST"])
@login_required
def admin_order_resend_telegram(order_id):
    if not admin_required(): return ("Forbidden",403)
    o=db.session.get(Order, order_id)
    if not o or not o.delivery: return ("Not found",404)
    if not o.user.telegram_id:
        flash("Customer has no Telegram linked.")
        return redirect(url_for("admin_order_detail", order_id=order_id))
    sent=send_telegram_chat(o.user.telegram_id, f"📦 Veyra delivery for order {public_order_ref(o)}\n\n{o.delivery.content}\n\nYour order is completed.")
    o.delivery.sent_to_customer_telegram=sent
    db.session.commit()
    flash("Delivery resent to customer Telegram." if sent else "Telegram delivery failed.")
    return redirect(url_for("admin_order_detail", order_id=order_id))

@app.route("/admin/customers/add", methods=["POST"])
@login_required
def admin_customer_add():
    if not admin_required(): return ("Forbidden", 403)
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()
    if not username:
        flash("Username / customer name is required.")
        return redirect(url_for("admin") + "#customers")
    username = username[:40]
    if User.query.filter(db.func.lower(User.username) == username.lower()).first():
        flash("That username already exists.")
        return redirect(url_for("admin") + "#customers")
    if email and User.query.filter(db.func.lower(User.email) == email.lower()).first():
        flash("That email already exists.")
        return redirect(url_for("admin") + "#customers")
    if not email:
        email = f"manual.{secrets.token_hex(8)}@customer.veyra.local"
    if not password:
        password = secrets.token_urlsafe(18)
    u = User(username=username, email=email, password_hash=generate_password_hash(password), is_admin=False)
    db.session.add(u)
    db.session.commit()
    audit("Customer created", f"@{u.username}")
    db.session.commit()
    flash(f"Customer @{u.username} was created.")
    return redirect(url_for("admin") + "#customers")


def _delete_customer_data(user_id):
    """Remove a customer and all customer-owned transactional/support data safely."""
    # Null admin/audit references first.
    AuditLog.query.filter_by(admin_user_id=user_id).update({AuditLog.admin_user_id: None}, synchronize_session=False)
    V21CatalogBackup.query.filter_by(created_by=user_id).update({V21CatalogBackup.created_by: None}, synchronize_session=False)

    # Support: replies can point to the customer, tickets own their replies.
    if "V21TicketReply" in globals():
        V21TicketReply.query.filter_by(user_id=user_id).update({V21TicketReply.user_id: None}, synchronize_session=False)
    if "V21Ticket" in globals():
        tickets = V21Ticket.query.filter_by(user_id=user_id).all()
        for t in tickets:
            t.order_id = None
        db.session.flush()
        for t in tickets:
            db.session.delete(t)
        db.session.flush()
    if "V21Notification" in globals(): V21Notification.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    if "V21RecentlyViewed" in globals(): V21RecentlyViewed.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    if "TelegramProfile" in globals(): TelegramProfile.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    TelegramLink.query.filter_by(user_id=user_id).delete(synchronize_session=False)

    # Customer-owned commerce data.
    CouponUsage.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Wishlist.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Review.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    LoyaltyPoint.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    Referral.query.filter((Referral.referrer_id == user_id) | (Referral.referred_id == user_id)).delete(synchronize_session=False)

    # Orders must be removed before the user because user_id is non-nullable.
    orders = Order.query.filter_by(user_id=user_id).all()
    for o in orders:
        if "V21Ticket" in globals():
            V21Ticket.query.filter_by(order_id=o.id).update({V21Ticket.order_id: None}, synchronize_session=False)
        OrderDelivery.query.filter_by(order_id=o.id).delete(synchronize_session=False)
        Subscription.query.filter_by(order_id=o.id).delete(synchronize_session=False)
        CouponUsage.query.filter_by(order_id=o.id).update({CouponUsage.order_id: None}, synchronize_session=False)
        db.session.delete(o)
    db.session.flush()

    Subscription.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    u = db.session.get(User, user_id)
    if u:
        db.session.delete(u)
    db.session.flush()

@app.route("/admin/customers/delete", methods=["POST"])
@login_required
def admin_customers_delete():
    if not admin_required(): return ("Forbidden", 403)
    raw_ids = request.form.getlist("user_ids")
    ids = []
    for raw in raw_ids:
        try:
            uid = int(raw)
            if uid not in ids: ids.append(uid)
        except (TypeError, ValueError):
            pass
    if not ids:
        flash("Select at least one customer.")
        return redirect(url_for("admin") + "#customers")
    deleted=[]
    try:
        for uid in ids:
            u=db.session.get(User, uid)
            if not u or u.is_admin:
                continue
            name=u.username
            _delete_customer_data(uid)
            deleted.append(name)
        db.session.commit()
        audit("Customers deleted", ", ".join("@"+x for x in deleted) if deleted else "No customers deleted")
        db.session.commit()
        flash(f"Deleted {len(deleted)} customer(s). Their orders, subscriptions and support data were also removed.")
    except Exception as exc:
        db.session.rollback()
        app.logger.exception("Customer delete failed")
        flash(f"Could not delete customers: {exc}")
    return redirect(url_for("admin") + "#customers")

@app.route("/admin/customer/<int:user_id>")
@login_required
def admin_customer_detail(user_id):
    if not admin_required(): return ("Forbidden",403)
    u=db.session.get(User,user_id)
    if not u or u.is_admin: return ("Not found",404)
    orders=Order.query.filter_by(user_id=u.id).order_by(Order.created_at.desc()).all()
    subs=Subscription.query.filter_by(user_id=u.id).order_by(Subscription.expires_at.desc()).all()
    points=db.session.query(db.func.coalesce(db.func.sum(LoyaltyPoint.points),0)).filter(LoyaltyPoint.user_id==u.id).scalar() or 0
    wishlist_items=Wishlist.query.filter_by(user_id=u.id).all()
    referrals=Referral.query.filter_by(referrer_id=u.id).all()
    reviews=Review.query.filter_by(user_id=u.id).order_by(Review.created_at.desc()).all()
    return render_template("admin_customer.html", customer=u, orders=orders, subscriptions=subs, points=points, wishlist_items=wishlist_items, referrals=referrals, reviews=reviews)

@app.route("/admin/orders/reset", methods=["POST"])
@login_required
def admin_orders_reset():
    """Reset transactional/test purchase data while preserving the catalog and customer accounts."""
    if not admin_required(): return ("Forbidden", 403)
    # Detach support tickets first because they may reference an order.
    if "V21Ticket" in globals():
        V21Ticket.query.update({V21Ticket.order_id: None}, synchronize_session=False)
    # Remove dependent transactional records first so this works on PostgreSQL and SQLite.
    delivery_count = OrderDelivery.query.delete(synchronize_session=False)
    subscription_count = Subscription.query.delete(synchronize_session=False)
    coupon_usage_count = CouponUsage.query.delete(synchronize_session=False)
    order_count = Order.query.delete(synchronize_session=False)
    # Test purchase rewards are transactional too; keep customers/products/settings intact.
    loyalty_count = LoyaltyPoint.query.delete(synchronize_session=False)
    Coupon.query.update({Coupon.used_count: 0}, synchronize_session=False)
    db.session.commit()
    audit("Purchase reset", f"Deleted {order_count} orders, {delivery_count} deliveries, {subscription_count} subscriptions, {coupon_usage_count} coupon uses and {loyalty_count} loyalty entries")
    db.session.commit()
    flash(f"Purchase test data reset: {order_count} orders removed. Products, plans and customers were preserved.")
    return redirect(url_for("admin") + "#orders")

@app.route("/admin/order/<int:order_id>/delete", methods=["POST"])
@login_required
def admin_order_delete(order_id):
    if not admin_required(): return ("Forbidden", 403)
    o = db.session.get(Order, order_id)
    if not o: return ("Not found", 404)
    public_label = public_order_ref(o)
    try:
        # Detach every known child record before deleting the parent order.
        # This is deliberately explicit for PostgreSQL foreign-key safety.
        if "V21Ticket" in globals():
            V21Ticket.query.filter_by(order_id=o.id).update({V21Ticket.order_id: None}, synchronize_session=False)
        OrderDelivery.query.filter_by(order_id=o.id).delete(synchronize_session=False)
        Subscription.query.filter_by(order_id=o.id).delete(synchronize_session=False)
        CouponUsage.query.filter_by(order_id=o.id).update({CouponUsage.order_id: None}, synchronize_session=False)
        db.session.flush()
        db.session.delete(o)
        db.session.flush()
        db.session.commit()
        audit("Order deleted", public_label)
        db.session.commit()
        flash(f"{public_label} was permanently removed.")
    except Exception as exc:
        db.session.rollback()
        app.logger.exception("Order delete failed for %s", order_id)
        flash(f"Could not delete {public_label}: {exc}")
    return redirect(url_for("admin") + "#orders")

@app.route("/admin/order/<int:order_id>/<status>", methods=["POST"])
@login_required
def order_status(order_id,status):
    if not admin_required(): return ("Forbidden",403)
    if status not in {"pending","processing","completed","cancelled"}: return ("Bad status",400)
    o = db.session.get(Order,order_id)
    if not o: return ("Not found",404)
    previous = o.status
    if status == "completed" and previous != "completed":
        complete_order_record(o, source="admin")
    else:
        o.status = status
    audit("Order status", f"{public_order_ref(o)}: {previous} → {status}")
    db.session.commit()
    if status == "completed" and previous != "completed":
        notify_admins(f"✅ {public_order_ref(o)} completed\n@{o.user.username}\n€{o.sale_price:.2f}\nProfit: €{o.profit:.2f}")
    return redirect(url_for("admin"))


@app.route("/admin/coupon/new", methods=["POST"])
@login_required
def admin_coupon_new():
    if not admin_required(): return ("Forbidden",403)
    code = request.form.get("code", "").strip().upper()
    if not code:
        flash("Coupon code is required.")
        return redirect(url_for("admin") + "#marketing")
    percent = max(0.0, float(request.form.get("discount_percent",0) or 0))
    fixed = max(0.0, float(request.form.get("discount_fixed",0) or 0))
    if percent <= 0 and fixed <= 0:
        flash("Set a percentage or fixed discount.")
        return redirect(url_for("admin") + "#marketing")
    if percent > 100:
        flash("Percentage discount cannot exceed 100%.")
        return redirect(url_for("admin") + "#marketing")
    if Coupon.query.filter_by(code=code).first():
        flash("Coupon already exists.")
        return redirect(url_for("admin") + "#marketing")
    expires_raw = request.form.get("expires_at", "").strip()
    expires = None
    if expires_raw:
        try: expires = datetime.fromisoformat(expires_raw)
        except ValueError: expires = None
    c = Coupon(code=code, discount_percent=percent,
               discount_fixed=fixed,
               max_uses=int(request.form.get("max_uses",0) or 0), expires_at=expires, active=request.form.get("active")=="on")
    db.session.add(c); audit("Coupon created", code); db.session.commit()
    flash(f"Coupon {code} created.")
    return redirect(url_for("admin") + "#marketing")

@app.route("/admin/coupon/<int:coupon_id>/toggle", methods=["POST"])
@login_required
def admin_coupon_toggle(coupon_id):
    if not admin_required(): return ("Forbidden",403)
    c=db.session.get(Coupon,coupon_id)
    if not c: return ("Not found",404)
    c.active=not c.active; audit("Coupon toggled", f"{c.code} → {c.active}"); db.session.commit()
    return redirect(url_for("admin") + "#marketing")

@app.route("/admin/coupon/<int:coupon_id>/delete", methods=["POST"])
@login_required
def admin_coupon_delete(coupon_id):
    if not admin_required(): return ("Forbidden",403)
    c=db.session.get(Coupon,coupon_id)
    if not c: return ("Not found",404)
    db.session.delete(c); audit("Coupon deleted", c.code); db.session.commit()
    return redirect(url_for("admin") + "#marketing")

@app.route("/admin/rewards", methods=["POST"])
@login_required
def admin_rewards_save():
    if not admin_required(): return ("Forbidden",403)
    for key, default in (("loyalty_points_per_eur", "1"), ("referral_reward_points", "100")):
        value = request.form.get(key, default).strip()
        try:
            number = float(value)
            if number < 0: raise ValueError
            if key == "referral_reward_points": value = str(int(number))
        except ValueError:
            flash(f"Invalid value for {key}.")
            return redirect(url_for("admin") + "#marketing")
        row = AdminSetting.query.filter_by(key=key).first()
        if not row:
            row = AdminSetting(key=key)
            db.session.add(row)
        row.value = value
    audit("Rewards settings updated", "Loyalty points per EUR and referral reward")
    db.session.commit()
    flash("Rewards settings saved.")
    return redirect(url_for("admin") + "#marketing")

@app.route("/admin/settings", methods=["POST"])
@login_required
def admin_settings_save():
    if not admin_required(): return ("Forbidden",403)
    keys = ["store_name","support_whatsapp","support_email","maintenance_mode","default_language","hero_title_en","hero_title_de","hero_title_fr","hero_title_sq","announcement","store_logo_url","default_theme","payment_provider","stripe_publishable_key","notify_new_order","notify_payment","notify_completion","notify_expiry","notify_review","notify_referral"]
    for key in keys:
        value = request.form.get(key, "").strip()
        row = AdminSetting.query.filter_by(key=key).first()
        if not row:
            row=AdminSetting(key=key)
            db.session.add(row)
        row.value=value
    audit("Store settings updated", ", ".join(keys)); db.session.commit()
    flash("Store settings saved.")
    return redirect(url_for("admin") + "#settings")

@app.route("/admin/telegram/save", methods=["POST"])
@login_required
def admin_telegram_save():
    if not admin_required(): return ("Forbidden",403)
    admins=User.query.filter_by(is_admin=True).order_by(User.id).limit(2).all()
    ids=[request.form.get("admin1_telegram_id","").strip(), request.form.get("admin2_telegram_id","").strip()]
    for i,u in enumerate(admins):
        u.telegram_id = ids[i] or None
    audit("Telegram admin IDs updated", "Two-admin allowlist"); db.session.commit()
    flash("Telegram admin IDs saved. Bot token remains in environment variables for security.")
    return redirect(url_for("admin") + "#telegram")

@app.route("/admin/telegram/test", methods=["POST"])
@login_required
def admin_telegram_test():
    if not admin_required(): return ("Forbidden",403)
    ok, err = send_telegram("✅ Veyra Telegram test\nYour admin notifications are connected.")
    flash("Telegram test sent successfully." if ok else f"Telegram test failed: {err}")
    return redirect(url_for("admin") + "#telegram")

@app.route("/admin/audit/clear", methods=["POST"])
@login_required
def admin_audit_clear():
    if not admin_required(): return ("Forbidden",403)
    AuditLog.query.delete(); audit("Audit log cleared"); db.session.commit()
    flash("Audit log cleared.")
    return redirect(url_for("admin") + "#activity")


def make_slug(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "product"

def unique_slug(text, model=Product, current_id=None):
    base = make_slug(text)
    slug = base
    n = 2
    while True:
        q = model.query.filter_by(slug=slug)
        if current_id is not None:
            q = q.filter(model.id != current_id)
        if not q.first():
            return slug
        slug = f"{base}-{n}"
        n += 1

def save_product_image(file_obj):
    if not file_obj or not getattr(file_obj, "filename", ""):
        return ""
    allowed = {"png","jpg","jpeg","webp","gif","svg"}
    ext = file_obj.filename.rsplit(".",1)[-1].lower() if "." in file_obj.filename else ""
    if ext not in allowed:
        return ""
    filename = secure_filename(file_obj.filename)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    filename = f"{stamp}-{filename}"
    file_obj.save(os.path.join(UPLOAD_DIR, filename))
    return url_for("static", filename=f"uploads/{filename}")

@app.route("/admin/product/new", methods=["POST"])
@login_required
def admin_product_new():
    if not admin_required(): return ("Forbidden",403)
    try:
        v21_save_catalog_backup("Before catalog change", "auto")
    except Exception:
        pass
    name = request.form.get("name", "").strip()
    category = db.session.get(Category, int(request.form.get("category_id", 0) or 0))
    if not name or not category:
        flash("Product name and category are required.")
        return redirect(url_for("admin"))
    uploaded_image = save_product_image(request.files.get("image_file"))
    p = Product(name=name, slug=unique_slug(request.form.get("slug") or name),
                description=request.form.get("description", "").strip(),
                price_label=(request.form.get("price_label_custom", "").strip() if request.form.get("price_label_type") == "custom" else request.form.get("price_label_type", "month").strip()) or "month",
                image_url=uploaded_image or request.form.get("image_url", "").strip(),
                category_id=category.id,
                active=request.form.get("active") == "on",
                featured=request.form.get("featured") == "on",
                sort_order=max(0, int(request.form.get("sort_order", 0) or 0)))
    db.session.add(p); db.session.flush()
    months = int(request.form.get("months", 1) or 1)
    price = float(request.form.get("price", 0) or 0)
    sale_raw = request.form.get("sale_price", "").strip()
    cost = float(request.form.get("cost_price", 0) or 0)
    db.session.add(Plan(product_id=p.id, name=f"{months} Month" + ("s" if months != 1 else ""),
                        months=months, price=price,
                        sale_price=float(sale_raw) if sale_raw else None,
                        cost_price=cost, active=True))
    db.session.commit()
    flash(f"Product '{p.name}' created.")
    return redirect(url_for("admin"))

@app.route("/admin/product/<int:product_id>/edit", methods=["POST"])
@login_required
def admin_product_edit(product_id):
    if not admin_required(): return ("Forbidden",403)
    try:
        v21_save_catalog_backup("Before catalog change", "auto")
    except Exception:
        pass
    p = db.session.get(Product, product_id)
    if not p: return ("Not found",404)
    name = request.form.get("name", p.name).strip()
    category_id = int(request.form.get("category_id", p.category_id) or p.category_id)
    category = db.session.get(Category, category_id)
    if not name or not category:
        flash("Product name and category are required.")
        return redirect(url_for("admin"))
    p.name = name
    p.slug = unique_slug(request.form.get("slug") or name, Product, p.id)
    p.description = request.form.get("description", "").strip()
    p.card_label = request.form.get("card_label", "").strip()[:140]
    p.price_label = (request.form.get("price_label_custom", "").strip() if request.form.get("price_label_type") == "custom" else request.form.get("price_label_type", p.price_label or "month").strip()) or "month"
    uploaded_image = save_product_image(request.files.get("image_file"))
    p.image_url = uploaded_image or request.form.get("image_url", "").strip()
    p.category_id = category.id
    p.active = request.form.get("active") == "on"
    p.featured = request.form.get("featured") == "on"
    try: p.sort_order = max(0, int(request.form.get("sort_order", p.sort_order) or p.sort_order))
    except (TypeError, ValueError): pass
    db.session.commit()
    flash(f"Product '{p.name}' updated.")
    return redirect(url_for("admin"))

@app.route("/admin/products/bulk-price", methods=["POST"])
@login_required
def admin_products_bulk_price():
    if not admin_required(): return ("Forbidden",403)
    try:
        v21_save_catalog_backup("Before catalog change", "auto")
    except Exception:
        pass
    product_ids = []
    for raw in request.form.getlist("product_ids"):
        try:
            product_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    product_ids = list(dict.fromkeys(product_ids))
    if not product_ids:
        flash("Select at least one product.")
        return redirect(url_for("admin") + "#products")

    plan_months_raw = request.form.get("plan_months", "all").strip().lower()
    try:
        public_price = float(request.form.get("price", ""))
    except (TypeError, ValueError):
        flash("Enter a valid public price.")
        return redirect(url_for("admin") + "#products")
    if public_price < 0:
        flash("Price cannot be negative.")
        return redirect(url_for("admin") + "#products")

    sale_raw = request.form.get("sale_price", "").strip()
    cost_raw = request.form.get("cost_price", "").strip()
    try:
        sale_price = float(sale_raw) if sale_raw else None
        cost_price = float(cost_raw) if cost_raw else None
    except ValueError:
        flash("Sale price and cost must be valid numbers.")
        return redirect(url_for("admin") + "#products")
    if sale_price is not None and sale_price < 0 or cost_price is not None and cost_price < 0:
        flash("Sale price and cost cannot be negative.")
        return redirect(url_for("admin") + "#products")

    selected = Product.query.filter(Product.id.in_(product_ids)).all()
    selected_by_id = {p.id: p for p in selected}
    changed = 0
    products_changed = []
    for pid in product_ids:
        p = selected_by_id.get(pid)
        if not p:
            continue
        plans = list(p.plans)
        if plan_months_raw != "all":
            try:
                target_months = int(plan_months_raw)
            except ValueError:
                continue
            plans = [plan for plan in plans if plan.months == target_months]
        for plan in plans:
            plan.price = public_price
            if sale_price is not None:
                plan.sale_price = sale_price
            if cost_price is not None:
                plan.cost_price = cost_price
            changed += 1
        if plans:
            products_changed.append(p.name)

    if changed:
        target = "all plans" if plan_months_raw == "all" else f"{plan_months_raw} month plan"
        audit("Bulk pricing update", f"{', '.join(products_changed)} → {target}: €{public_price:.2f}" + (f", sale €{sale_price:.2f}" if sale_price is not None else "") + (f", cost €{cost_price:.2f}" if cost_price is not None else ""))
        db.session.commit()
        flash(f"Updated {changed} plan(s) across {len(products_changed)} product(s).")
    else:
        flash("No matching plans found for the selected products.")
    return redirect(url_for("admin") + "#products")

@app.route("/admin/product/<int:product_id>/delete", methods=["POST"])
@login_required
def admin_product_delete(product_id):
    if not admin_required(): return ("Forbidden",403)
    try:
        v21_save_catalog_backup("Before catalog change", "auto")
    except Exception:
        pass
    p = db.session.get(Product, product_id)
    if not p: return ("Not found",404)

    # This is a true permanent delete. Visibility is handled separately by Hide/Show.
    # Remove every dependent record first so PostgreSQL/SQLite cannot silently force an archive.
    plan_ids = [x.id for x in p.plans]
    order_ids = [x.id for x in Order.query.filter(Order.plan_id.in_(plan_ids)).all()] if plan_ids else []
    if order_ids:
        OrderDelivery.query.filter(OrderDelivery.order_id.in_(order_ids)).delete(synchronize_session=False)
        Subscription.query.filter(Subscription.order_id.in_(order_ids)).delete(synchronize_session=False)
        CouponUsage.query.filter(CouponUsage.order_id.in_(order_ids)).delete(synchronize_session=False)
        Order.query.filter(Order.id.in_(order_ids)).delete(synchronize_session=False)

    if plan_ids:
        # Remove V21 references first so PostgreSQL/SQLite foreign keys cannot block deletion.
        # V21 metadata/history is additive and safe to clean up with the product.
        if "V21DealItem" in globals():
            v21_deal_ids = [x.deal_id for x in V21DealItem.query.filter(V21DealItem.plan_id.in_(plan_ids)).all()]
            V21DealItem.query.filter(V21DealItem.plan_id.in_(plan_ids)).delete(synchronize_session=False)
            for did in v21_deal_ids:
                d = db.session.get(V21Deal, did)
                if d and not V21DealItem.query.filter_by(deal_id=did).first():
                    if d.legacy_bundle_id and Order.query.filter_by(bundle_id=d.legacy_bundle_id).first():
                        d.active = False
                    else:
                        db.session.delete(d)
        # Remove the product's plans from legacy bundles. Empty bundles are deleted; bundles with
        # their own order history are kept but disabled so their historical records remain valid.
        bundle_ids = [x.bundle_id for x in BundleItem.query.filter(BundleItem.plan_id.in_(plan_ids)).all()]
        BundleItem.query.filter(BundleItem.plan_id.in_(plan_ids)).delete(synchronize_session=False)
        for bid in bundle_ids:
            b = db.session.get(Bundle, bid)
            if b and not BundleItem.query.filter_by(bundle_id=bid).first():
                if not Order.query.filter_by(bundle_id=bid).first():
                    db.session.delete(b)
                else:
                    b.active = False

        Subscription.query.filter(Subscription.product_id == p.id).delete(synchronize_session=False)
        db.session.execute(db.delete(Plan).where(Plan.id.in_(plan_ids)))

    Review.query.filter_by(product_id=p.id).delete(synchronize_session=False)
    Wishlist.query.filter_by(product_id=p.id).delete(synchronize_session=False)
    # Clean V21 product-level references as well.
    if "V21ProductMeta" in globals():
        V21ProductMeta.query.filter_by(product_id=p.id).delete(synchronize_session=False)
    if "V21RecentlyViewed" in globals():
        V21RecentlyViewed.query.filter_by(product_id=p.id).delete(synchronize_session=False)
    if "V21CouponRule" in globals():
        V21CouponRule.query.filter_by(product_id=p.id).delete(synchronize_session=False)
    name = p.name
    db.session.delete(p)
    db.session.commit()
    audit("Product permanently deleted", f"{name} and its plans/dependent purchase records")
    db.session.commit()
    flash(f"'{name}' was permanently deleted. It is no longer hidden/archived.")
    return redirect(url_for("admin") + "#products")

@app.route("/admin/plan/new", methods=["POST"])
@login_required
def admin_plan_new():
    if not admin_required(): return ("Forbidden",403)
    product = db.session.get(Product, int(request.form.get("product_id", 0) or 0))
    if not product: return ("Product not found",404)
    months = int(request.form.get("months", 1) or 1)
    label = request.form.get("name", "").strip() or f"{months} Month" + ("s" if months != 1 else "")
    sale_raw = request.form.get("sale_price", "").strip()
    plan = Plan(product_id=product.id, name=label, months=months,
                price=float(request.form.get("price", 0) or 0),
                sale_price=float(sale_raw) if sale_raw else None,
                cost_price=float(request.form.get("cost_price", 0) or 0),
                active=request.form.get("active") == "on",
                sort_order=max(0, int(request.form.get("sort_order", 0) or 0)))
    db.session.add(plan); db.session.commit()
    flash(f"Plan added to {product.name}.")
    return redirect(url_for("admin"))

@app.route("/admin/plan/<int:plan_id>/edit", methods=["POST"])
@login_required
def admin_plan_edit(plan_id):
    if not admin_required(): return ("Forbidden",403)
    plan = db.session.get(Plan, plan_id)
    if not plan: return ("Not found",404)
    plan.name = request.form.get("name", plan.name).strip() or plan.name
    plan.months = int(request.form.get("months", plan.months) or plan.months)
    plan.price = float(request.form.get("price", plan.price) or 0)
    sale_raw = request.form.get("sale_price", "").strip()
    plan.sale_price = float(sale_raw) if sale_raw else None
    plan.cost_price = float(request.form.get("cost_price", plan.cost_price) or 0)
    plan.active = request.form.get("active") == "on"
    try: plan.sort_order = max(0, int(request.form.get("sort_order", plan.sort_order) or plan.sort_order))
    except (TypeError, ValueError): pass
    db.session.commit()
    flash(f"{plan.product.name} — {plan.name} updated.")
    return redirect(url_for("admin"))

@app.route("/admin/plan/<int:plan_id>/delete", methods=["POST"])
@login_required
def admin_plan_delete(plan_id):
    if not admin_required(): return ("Forbidden",403)
    plan = db.session.get(Plan, plan_id)
    if not plan: return ("Not found",404)
    if Order.query.filter_by(plan_id=plan.id).first() or BundleItem.query.filter_by(plan_id=plan.id).first():
        plan.active = False
        db.session.commit()
        flash(f"{plan.name} was archived because it is used in history or a bundle.")
    else:
        db.session.delete(plan); db.session.commit(); flash("Plan deleted.")
    return redirect(url_for("admin"))

@app.route("/admin/product/<int:product_id>/toggle", methods=["POST"])
@login_required
def admin_product_toggle(product_id):
    if not admin_required(): return ("Forbidden", 403)
    p = db.session.get(Product, product_id)
    if not p: return ("Not found", 404)
    p.active = not p.active
    for plan in p.plans:
        if p.active and not plan.active:
            # Product visibility is independent from plan visibility; do not
            # silently re-enable archived plans.
            pass
    audit("Product visibility", f"{p.name} → {'active' if p.active else 'hidden'}")
    db.session.commit()
    return redirect(url_for("admin") + "#products")

@app.route("/admin/product/<int:product_id>/duplicate", methods=["POST"])
@login_required
def admin_product_duplicate(product_id):
    if not admin_required(): return ("Forbidden", 403)
    source = db.session.get(Product, product_id)
    if not source: return ("Not found", 404)
    copy = Product(name=f"{source.name} Copy", slug=unique_slug(f"{source.name}-copy"),
                    price_label=source.price_label or "month",
                   description=source.description, image_url=source.image_url,
                   category_id=source.category_id, active=False, featured=False)
    db.session.add(copy); db.session.flush()
    for plan in source.plans:
        db.session.add(Plan(product_id=copy.id, name=plan.name, months=plan.months,
                             price=plan.price, sale_price=plan.sale_price,
                             cost_price=plan.cost_price, active=plan.active))
    audit("Product duplicated", f"{source.name} → {copy.name}")
    db.session.commit()
    flash(f"{source.name} u kopjua si draft.")
    return redirect(url_for("admin") + "#products")

@app.route("/admin/plan/<int:plan_id>/toggle", methods=["POST"])
@login_required
def admin_plan_toggle(plan_id):
    if not admin_required(): return ("Forbidden", 403)
    plan = db.session.get(Plan, plan_id)
    if not plan: return ("Not found", 404)
    plan.active = not plan.active
    audit("Plan visibility", f"{plan.product.name} / {plan.name} → {'active' if plan.active else 'hidden'}")
    db.session.commit()
    return redirect(url_for("admin") + "#products")

@app.route("/admin/bundle/<int:bundle_id>/toggle", methods=["POST"])
@login_required
def admin_bundle_toggle(bundle_id):
    if not admin_required(): return ("Forbidden", 403)
    b = db.session.get(Bundle, bundle_id)
    if not b: return ("Not found", 404)
    b.active = not b.active
    audit("Bundle visibility", f"{b.name} → {'active' if b.active else 'hidden'}")
    db.session.commit()
    return redirect(url_for("admin") + "#bundles")

@app.route("/admin/subscription/<int:subscription_id>/<action>", methods=["POST"])
@login_required
def admin_subscription_action(subscription_id, action):
    if not admin_required(): return ("Forbidden", 403)
    sub = db.session.get(Subscription, subscription_id)
    if not sub: return ("Not found", 404)
    if action == "extend":
        base = max(datetime.utcnow(), sub.expires_at)
        sub.expires_at = base + timedelta(days=30)
        sub.active = True
        flash(f"{sub.product.name} u zgjat për 30 ditë.")
    elif action == "activate":
        sub.active = True
        if sub.expires_at <= datetime.utcnow():
            sub.expires_at = datetime.utcnow() + timedelta(days=30)
        flash("Abonimi u aktivizua.")
    elif action == "deactivate":
        sub.active = False
        flash("Abonimi u çaktivizua.")
    else:
        return ("Bad action", 400)
    audit("Subscription action", f"#{sub.id} {action}")
    db.session.commit()
    return redirect(url_for("admin") + "#subscriptions")

@app.route("/admin/subscription/<int:subscription_id>/manage", methods=["POST"])
@login_required
def admin_subscription_manage(subscription_id):
    if not admin_required(): return ("Forbidden",403)
    sub=db.session.get(Subscription,subscription_id)
    if not sub: return ("Not found",404)
    action=request.form.get("action","extend")
    if action=="extend_days":
        try: days=max(1,min(3650,int(request.form.get("days","30"))))
        except (TypeError,ValueError): days=30
        base=max(datetime.utcnow(),sub.expires_at)
        sub.expires_at=base+timedelta(days=days);sub.active=True
        flash(f"{sub.product.name} u zgjat për {days} ditë.")
        audit("Subscription extended",f"#{sub.id} +{days} days")
    elif action=="change_plan":
        plan=db.session.get(Plan,request.form.get("plan_id",type=int))
        if not plan or plan.product_id!=sub.product_id:
            flash("Invalid plan for this product.")
            return redirect(url_for("admin")+"#subscriptions")
        sub.plan_id=plan.id
        flash(f"Plani u ndryshua në {plan.name}.")
        audit("Subscription plan changed",f"#{sub.id} → {plan.name}")
    elif action=="set_expiry":
        raw=request.form.get("expires_at","").strip()
        try:
            sub.expires_at=datetime.fromisoformat(raw)
            sub.active=True
            flash("Data e skadimit u ruajt.")
            audit("Subscription expiry changed",f"#{sub.id} → {sub.expires_at.isoformat()}")
        except ValueError:
            flash("Invalid expiry date.")
    else:
        return ("Bad action",400)
    db.session.commit()
    return redirect(url_for("admin")+"#subscriptions")

@app.route("/admin/reorder/products", methods=["POST"])
@login_required
def admin_reorder_products():
    if not admin_required(): return ("Forbidden", 403)
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    try: ids = [int(x) for x in ids]
    except (TypeError, ValueError): return ({"ok": False, "error": "Invalid product order"}, 400)
    products = {p.id: p for p in Product.query.filter(Product.id.in_(ids)).all()}
    for pos, pid in enumerate(ids):
        if pid in products: products[pid].sort_order = pos
    audit("Products reordered", f"{len(products)} products")
    db.session.commit()
    return {"ok": True}

@app.route("/admin/reorder/plans", methods=["POST"])
@login_required
def admin_reorder_plans():
    if not admin_required(): return ("Forbidden", 403)
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    product_id = data.get("product_id")
    try:
        ids = [int(x) for x in ids]
        product_id = int(product_id)
    except (TypeError, ValueError): return ({"ok": False, "error": "Invalid plan order"}, 400)
    plans = {p.id: p for p in Plan.query.filter_by(product_id=product_id).all()}
    for pos, pid in enumerate(ids):
        if pid in plans: plans[pid].sort_order = pos
    audit("Plans reordered", f"product_id={product_id}, {len(plans)} plans")
    db.session.commit()
    return {"ok": True}

@app.route("/admin/invoice/<int:order_id>.pdf")
@login_required
def admin_invoice_pdf(order_id):
    if not admin_required(): return ("Forbidden",403)
    o=db.session.get(Order,order_id)
    if not o: return ("Not found",404)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
    except ImportError:
        return ("Invoice PDF dependency is not installed.",500)
    import io
    product=o.plan.product.name if o.plan and o.plan.product else (o.bundle.name if o.bundle else "Digital product")
    plan=o.plan.name if o.plan else "Bundle"
    buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=A4); w,h=A4
    c.setTitle(f"Veyra Invoice #{o.id}")
    c.setFont("Helvetica-Bold",22); c.drawString(25*mm,h-30*mm,"VEYRA")
    c.setFont("Helvetica",10); c.drawString(25*mm,h-37*mm,"Digital marketplace")
    c.setFont("Helvetica-Bold",14); c.drawRightString(w-25*mm,h-30*mm,f"INVOICE #{o.id}")
    c.setFont("Helvetica",9); c.drawRightString(w-25*mm,h-37*mm,o.created_at.strftime("%d.%m.%Y %H:%M"))
    y=h-60*mm
    c.setFont("Helvetica-Bold",10); c.drawString(25*mm,y,"CUSTOMER")
    c.setFont("Helvetica",10); c.drawString(25*mm,y-6*mm,f"@{o.user.username}"); c.drawString(25*mm,y-12*mm,o.user.email)
    y-=32*mm
    c.setFont("Helvetica-Bold",10); c.drawString(25*mm,y,"ITEM"); c.drawRightString(w-25*mm,y,"AMOUNT")
    y-=8*mm; c.line(25*mm,y,w-25*mm,y); y-=9*mm
    c.setFont("Helvetica",10); c.drawString(25*mm,y,f"{product} · {plan}"); c.drawRightString(w-25*mm,y,f"EUR {o.sale_price:.2f}")
    y-=10*mm
    if o.discount:
        c.drawString(25*mm,y,"Discount"); c.drawRightString(w-25*mm,y,f"- EUR {o.discount:.2f}"); y-=8*mm
    c.line(110*mm,y,w-25*mm,y); y-=10*mm
    c.setFont("Helvetica-Bold",11); c.drawString(25*mm,y,"TOTAL"); c.drawRightString(w-25*mm,y,f"EUR {o.sale_price:.2f}")
    y-=18*mm; c.setFont("Helvetica",9); c.drawString(25*mm,y,f"Status: {o.status}")
    c.drawString(25*mm,y-6*mm,"Thank you for your purchase.")
    c.save(); buf.seek(0)
    return Response(buf.getvalue(),mimetype="application/pdf",headers={"Content-Disposition":f"attachment; filename=veyra-invoice-{o.id}.pdf"})

@app.route("/admin/export/orders.csv")
@login_required
def admin_export_orders():
    if not admin_required(): return ("Forbidden", 403)
    import csv, io
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Order","Customer","Product","Plan","Status","Sale","Cost","Profit","Coupon","Created"])
    for o in Order.query.order_by(Order.created_at.desc()).all():
        product = o.plan.product.name if o.plan else (o.bundle.name if o.bundle else "")
        plan = o.plan.name if o.plan else "Bundle"
        writer.writerow([o.id, o.user.username, product, plan, o.status, f"{o.sale_price:.2f}",
                         f"{o.cost_price:.2f}", f"{o.profit:.2f}", o.coupon_code or "",
                         o.created_at.isoformat()])
    from flask import Response
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=veyra-orders.csv"})

@app.route("/admin/category/new", methods=["POST"])
@login_required
def admin_category_new():
    if not admin_required(): return ("Forbidden",403)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Category name is required.")
        return redirect(url_for("admin"))
    slug = make_slug(request.form.get("slug") or name)
    if Category.query.filter((Category.name == name) | (Category.slug == slug)).first():
        flash("Category already exists.")
        return redirect(url_for("admin"))
    db.session.add(Category(name=name, slug=slug)); db.session.commit()
    flash(f"Category '{name}' created.")
    return redirect(url_for("admin"))

@app.route("/admin/category/<int:category_id>/edit", methods=["POST"])
@login_required
def admin_category_edit(category_id):
    if not admin_required(): return ("Forbidden",403)
    c = db.session.get(Category, category_id)
    if not c: return ("Not found",404)
    c.name = request.form.get("name", c.name).strip() or c.name
    c.slug = make_slug(request.form.get("slug") or c.name)
    db.session.commit(); flash("Category updated.")
    return redirect(url_for("admin"))

@app.route("/admin/category/<int:category_id>/delete", methods=["POST"])
@login_required
def admin_category_delete(category_id):
    if not admin_required(): return ("Forbidden",403)
    c = db.session.get(Category, category_id)
    if not c: return ("Not found",404)
    if c.products:
        flash(f"Cannot delete '{c.name}' while it still has products.")
    else:
        db.session.delete(c); db.session.commit(); flash("Category deleted.")
    return redirect(url_for("admin"))

@app.route("/admin/bundle/new", methods=["POST"])
@login_required
def admin_bundle_new():
    if not admin_required(): return ("Forbidden",403)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Bundle name is required.")
        return redirect(url_for("admin"))
    b = Bundle(name=name, description=request.form.get("description", "").strip(),
               price=float(request.form.get("price", 0) or 0),
               active=request.form.get("active") == "on",
               featured=request.form.get("featured") == "on")
    db.session.add(b); db.session.flush()
    for raw in request.form.getlist("plan_ids"):
        try: pid = int(raw)
        except ValueError: continue
        if db.session.get(Plan, pid): db.session.add(BundleItem(bundle_id=b.id, plan_id=pid, quantity=1))
    db.session.commit(); flash(f"Bundle '{b.name}' created.")
    return redirect(url_for("admin"))

@app.route("/admin/bundle/<int:bundle_id>/edit", methods=["POST"])
@login_required
def admin_bundle_edit(bundle_id):
    if not admin_required(): return ("Forbidden",403)
    b = db.session.get(Bundle, bundle_id)
    if not b: return ("Not found",404)
    b.name = request.form.get("name", b.name).strip() or b.name
    b.description = request.form.get("description", "").strip()
    b.price = float(request.form.get("price", b.price) or 0)
    b.active = request.form.get("active") == "on"
    b.featured = request.form.get("featured") == "on"
    BundleItem.query.filter_by(bundle_id=b.id).delete(synchronize_session=False)
    for raw in request.form.getlist("plan_ids"):
        try: pid = int(raw)
        except ValueError: continue
        if db.session.get(Plan, pid): db.session.add(BundleItem(bundle_id=b.id, plan_id=pid, quantity=1))
    db.session.commit(); flash(f"Bundle '{b.name}' updated.")
    return redirect(url_for("admin"))

@app.route("/admin/bundle/<int:bundle_id>/delete", methods=["POST"])
@login_required
def admin_bundle_delete(bundle_id):
    if not admin_required(): return ("Forbidden",403)
    b = db.session.get(Bundle, bundle_id)
    if not b: return ("Not found",404)
    if Order.query.filter_by(bundle_id=b.id).first():
        b.active = False; db.session.commit(); flash("Bundle archived because it has order history.")
    else:
        db.session.delete(b); db.session.commit(); flash("Bundle deleted.")
    return redirect(url_for("admin"))

with app.app_context():
    db.create_all()
    seed()
    start_telegram_bot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=True)


# ========================= VEYRA v2.1 ADDITIVE LAYER =========================
# This layer is intentionally additive: the existing ordering/payment routes are kept intact.
from sqlalchemy import Text

class V21ProductMeta(db.Model):
    __tablename__ = "v21_product_meta"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), unique=True, nullable=False)
    product_type = db.Column(db.String(50), default="Digital subscription")
    region = db.Column(db.String(120), default="Worldwide")
    delivery = db.Column(db.String(180), default="Fast digital delivery")
    guarantee = db.Column(db.String(180), default="Support included")
    receive_type = db.Column(db.String(50), default="Personal Account")
    included_json = db.Column(Text, default="[]")
    faq_json = db.Column(Text, default="[]")
    badges_json = db.Column(Text, default="[]")
    keywords = db.Column(db.String(500), default="")
    desc_i18n = db.Column(Text, default="{}")
    product = db.relationship("Product", backref=db.backref("v21_meta", uselist=False))

class V21PaymentMethod(db.Model):
    __tablename__ = "v21_payment_methods"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    enabled = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=0)

class V21Notification(db.Model):
    __tablename__ = "v21_notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(180), nullable=False)
    body = db.Column(Text, default="")
    kind = db.Column(db.String(40), default="account")
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")

class V21Ticket(db.Model):
    __tablename__ = "v21_tickets"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    subject = db.Column(db.String(180), nullable=False)
    category = db.Column(db.String(50), default="Other")
    status = db.Column(db.String(30), default="open")
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship("User")
    order = db.relationship("Order")
    replies = db.relationship("V21TicketReply", backref="ticket", cascade="all, delete-orphan", order_by="V21TicketReply.created_at")

class V21TicketReply(db.Model):
    __tablename__ = "v21_ticket_replies"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("v21_tickets.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    message = db.Column(Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")

class V21RecentlyViewed(db.Model):
    __tablename__ = "v21_recently_viewed"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    session_key = db.Column(db.String(120), nullable=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    viewed_at = db.Column(db.DateTime, default=datetime.utcnow)
    product = db.relationship("Product")

class V21Deal(db.Model):
    __tablename__ = "v21_deals"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(Text, default="")
    price = db.Column(db.Float, nullable=False, default=0)
    active = db.Column(db.Boolean, default=True)
    featured = db.Column(db.Boolean, default=False)
    starts_at = db.Column(db.DateTime, nullable=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    legacy_bundle_id = db.Column(db.Integer, db.ForeignKey("bundle.id"), nullable=True)
    items = db.relationship("V21DealItem", backref="deal", cascade="all, delete-orphan")

class V21DealItem(db.Model):
    __tablename__ = "v21_deal_items"
    id = db.Column(db.Integer, primary_key=True)
    deal_id = db.Column(db.Integer, db.ForeignKey("v21_deals.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    plan = db.relationship("Plan")

class V21CouponRule(db.Model):
    __tablename__ = "v21_coupon_rules"
    id = db.Column(db.Integer, primary_key=True)
    coupon_id = db.Column(db.Integer, db.ForeignKey("coupon.id"), unique=True, nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=True)
    enabled = db.Column(db.Boolean, default=True)
    coupon = db.relationship("Coupon", backref=db.backref("v21_rule", uselist=False))

with app.app_context():
    db.create_all()

V21_LANG = {
    "en": {"Digital subscription":"Digital subscription","Worldwide":"Worldwide","Fast digital delivery":"Fast digital delivery","Support included":"Support included","Personal Account":"Personal Account","Account Upgrade":"Account Upgrade","Shared Account":"Shared Account","Shared Profile":"Shared Profile","Activation Key":"Activation Key","Overview":"Overview","Pricing":"Pricing","What's included":"What's included","Delivery":"Delivery","Guarantee":"Guarantee","Region":"Region","FAQ":"FAQ","Buy Now":"Buy Now","Renew":"Renew","Expiring Soon":"Expiring Soon","Days remaining":"Days remaining","My Orders":"My Orders","My Subscriptions":"My Subscriptions","Notifications":"Notifications","Account Settings":"Account Settings","Support Tickets":"Support Tickets","Recently Viewed":"Recently Viewed","Savings":"Savings","Amount saved":"Amount saved","Best Seller":"BEST SELLER","Popular":"POPULAR","New":"NEW","Best Value":"BEST VALUE","Instant Delivery":"INSTANT DELIVERY","Premium":"PREMIUM","Limited Offer":"LIMITED OFFER","Create Ticket":"Create Ticket","Order Issue":"Order Issue","Account Issue":"Account Issue","Payment Issue":"Payment Issue","Product Question":"Product Question","Other":"Other","Open":"Open","Pending":"Pending","Resolved":"Resolved","Payment methods":"Payment methods","Enabled":"Enabled","Disabled":"Disabled","Save changes":"Save changes","Product metadata":"Product metadata","Deals & bundles":"Deals & bundles","Coupon rules":"Coupon rules","Smart search":"Smart search","No products found":"No products found","Personal account":"Personal account"},
    "de": {"Digital subscription":"Digitales Abonnement","Worldwide":"Weltweit","Fast digital delivery":"Schnelle digitale Lieferung","Support included":"Support inklusive","Personal Account":"Persönliches Konto","Account Upgrade":"Konto-Upgrade","Shared Account":"Geteiltes Konto","Shared Profile":"Geteiltes Profil","Activation Key":"Aktivierungsschlüssel","Overview":"Übersicht","Pricing":"Preise","What's included":"Enthalten","Delivery":"Lieferung","Guarantee":"Garantie","Region":"Region","FAQ":"FAQ","Buy Now":"Jetzt kaufen","Renew":"Verlängern","Expiring Soon":"Läuft bald ab","Days remaining":"Tage verbleibend","My Orders":"Meine Bestellungen","My Subscriptions":"Meine Abonnements","Notifications":"Benachrichtigungen","Account Settings":"Kontoeinstellungen","Support Tickets":"Support-Tickets","Recently Viewed":"Zuletzt angesehen","Savings":"Ersparnis","Amount saved":"Gespart","Best Seller":"BESTSELLER","Popular":"BELIEBT","New":"NEU","Best Value":"BESTES ANGEBOT","Instant Delivery":"SOFORT-LIEFERUNG","Premium":"PREMIUM","Limited Offer":"LIMITIERT","Create Ticket":"Ticket erstellen","Order Issue":"Bestellproblem","Account Issue":"Kontoproblem","Payment Issue":"Zahlungsproblem","Product Question":"Produktfrage","Other":"Sonstiges","Open":"Offen","Pending":"Ausstehend","Resolved":"Gelöst","Payment methods":"Zahlungsmethoden","Enabled":"Aktiv","Disabled":"Deaktiviert","Save changes":"Änderungen speichern","Product metadata":"Produktdaten","Deals & bundles":"Angebote & Bundles","Coupon rules":"Gutscheinregeln","Smart search":"Intelligente Suche","No products found":"Keine Produkte gefunden","Personal account":"Persönliches Konto"},
    "fr": {"Digital subscription":"Abonnement numérique","Worldwide":"Monde entier","Fast digital delivery":"Livraison numérique rapide","Support included":"Support inclus","Personal Account":"Compte personnel","Account Upgrade":"Mise à niveau du compte","Shared Account":"Compte partagé","Shared Profile":"Profil partagé","Activation Key":"Clé d’activation","Overview":"Aperçu","Pricing":"Tarifs","What's included":"Inclus","Delivery":"Livraison","Guarantee":"Garantie","Region":"Région","FAQ":"FAQ","Buy Now":"Acheter maintenant","Renew":"Renouveler","Expiring Soon":"Expire bientôt","Days remaining":"Jours restants","My Orders":"Mes commandes","My Subscriptions":"Mes abonnements","Notifications":"Notifications","Account Settings":"Paramètres du compte","Support Tickets":"Tickets support","Recently Viewed":"Vus récemment","Savings":"Économie","Amount saved":"Montant économisé","Best Seller":"MEILLEURE VENTE","Popular":"POPULAIRE","New":"NOUVEAU","Best Value":"MEILLEUR PRIX","Instant Delivery":"LIVRAISON INSTANTANÉE","Premium":"PREMIUM","Limited Offer":"OFFRE LIMITÉE","Create Ticket":"Créer un ticket","Order Issue":"Problème de commande","Account Issue":"Problème de compte","Payment Issue":"Problème de paiement","Product Question":"Question produit","Other":"Autre","Open":"Ouvert","Pending":"En attente","Resolved":"Résolu","Payment methods":"Modes de paiement","Enabled":"Activé","Disabled":"Désactivé","Save changes":"Enregistrer","Product metadata":"Données produit","Deals & bundles":"Offres & bundles","Coupon rules":"Règles de coupons","Smart search":"Recherche intelligente","No products found":"Aucun produit trouvé","Personal account":"Compte personnel"},
    "sq": {"Digital subscription":"Abonim digjital","Worldwide":"Në gjithë botën","Fast digital delivery":"Dorëzim i shpejtë digjital","Support included":"Mbështetja përfshihet","Personal Account":"Llogari personale","Account Upgrade":"Upgrade i llogarisë","Shared Account":"Llogari e përbashkët","Shared Profile":"Profil i përbashkët","Activation Key":"Çelës aktivizimi","Overview":"Përmbledhje","Pricing":"Çmimi","What's included":"Çfarë përfshihet","Delivery":"Dorëzimi","Guarantee":"Garancia","Region":"Regjioni","FAQ":"FAQ","Buy Now":"Bli tani","Renew":"Rinovo","Expiring Soon":"Skadon së shpejti","Days remaining":"Ditë të mbetura","My Orders":"Porositë e mia","My Subscriptions":"Abonimet e mia","Notifications":"Njoftimet","Account Settings":"Cilësimet e llogarisë","Support Tickets":"Tickets e supportit","Recently Viewed":"Shikuar së fundmi","Savings":"Kursimi","Amount saved":"Shuma e kursyer","Best Seller":"MË I SHITURI","Popular":"POPULAR","New":"I RI","Best Value":"VLERA MË E MIRË","Instant Delivery":"DORËZIM I MENJËHERSHËM","Premium":"PREMIUM","Limited Offer":"OFERTË E KUFIZUAR","Create Ticket":"Krijo ticket","Order Issue":"Problem me porosinë","Account Issue":"Problem me llogarinë","Payment Issue":"Problem me pagesën","Product Question":"Pyetje për produktin","Other":"Tjetër","Open":"Hapur","Pending":"Në pritje","Resolved":"Zgjidhur","Payment methods":"Metodat e pagesës","Enabled":"Aktive","Disabled":"Joaktive","Save changes":"Ruaj ndryshimet","Product metadata":"Të dhënat e produktit","Deals & bundles":"Oferta & bundle","Coupon rules":"Rregullat e kuponëve","Smart search":"Kërkim inteligjent","No products found":"Nuk u gjet asnjë produkt","Personal account":"Llogari personale"}
}

V21_EXTRA = {
"en":{"Available":"Available","Unavailable":"Unavailable","products":"products","ACCOUNT":"ACCOUNT","SHOP":"SHOP","OFFERS":"OFFERS","HISTORY":"HISTORY","What you receive":"What you receive","WHAT YOU RECEIVE":"WHAT YOU RECEIVE","This is shown before checkout so you know exactly what you are buying.":"This is shown before checkout so you know exactly what you are buying.","Everything you need":"Everything you need","Questions":"Questions","Subscription status":"Subscription status","Order history":"Order history","Latest":"Latest","Need help?":"Need help?","Status:":"Status:","Expired":"Expired","Mark all read":"Mark all read","Mark read":"Mark read","No notifications.":"No notifications.","How can we help?":"How can we help?","Subject":"Subject","Related order (optional)":"Related order (optional)","Describe the issue...":"Describe the issue...","Your tickets":"Your tickets","No tickets yet.":"No tickets yet.","Write a reply...":"Write a reply...","Send reply":"Send reply","Control center":"Control center","Future checkout methods":"Future checkout methods","All new methods are disabled by default. The existing payment/order routes remain unchanged.":"All new methods are disabled by default. The existing payment/order routes remain unchanged.","What customers see":"What customers see","Select product":"Select product","Search aliases e.g. spo, spotify, music":"Search aliases e.g. spo, spotify, music","What's included — one item per line":"What's included — one item per line","FAQ — one per line: Question | Answer":"FAQ — one per line: Question | Answer","Create a deal from existing plans":"Create a deal from existing plans","Deal name":"Deal name","Bundle price":"Bundle price","Description":"Description","Create deal":"Create deal","Scope existing coupons":"Scope existing coupons","All products":"All products","All categories":"All categories","History":"History"},
"de":{"Available":"Verfügbar","Unavailable":"Nicht verfügbar","products":"Produkte","ACCOUNT":"KONTO","SHOP":"SHOP","OFFERS":"ANGEBOTE","HISTORY":"VERLAUF","What you receive":"Was du erhältst","WHAT YOU RECEIVE":"WAS DU ERHÄLTST","This is shown before checkout so you know exactly what you are buying.":"Dies wird vor dem Checkout angezeigt, damit du genau weißt, was du kaufst.","Everything you need":"Alles, was du brauchst","Questions":"Fragen","Subscription status":"Abostatus","Order history":"Bestellverlauf","Latest":"Neueste","Need help?":"Brauchst du Hilfe?","Expired":"Abgelaufen","Mark all read":"Alle als gelesen markieren","Mark read":"Als gelesen markieren","No notifications.":"Keine Benachrichtigungen.","How can we help?":"Wie können wir helfen?","Subject":"Betreff","Related order (optional)":"Zugehörige Bestellung (optional)","Describe the issue...":"Beschreibe das Problem...","Your tickets":"Deine Tickets","No tickets yet.":"Noch keine Tickets.","Write a reply...":"Antwort schreiben...","Send reply":"Antwort senden","Future checkout methods":"Zukünftige Zahlungsmethoden","What customers see":"Was Kunden sehen","Select product":"Produkt auswählen","Create a deal from existing plans":"Angebot aus bestehenden Plänen erstellen","Deal name":"Angebotsname","Bundle price":"Bundle-Preis","Description":"Beschreibung","Create deal":"Angebot erstellen","Scope existing coupons":"Bestehende Gutscheine begrenzen","All products":"Alle Produkte","All categories":"Alle Kategorien"},
"fr":{"Available":"Disponible","Unavailable":"Indisponible","products":"produits","ACCOUNT":"COMPTE","SHOP":"BOUTIQUE","OFFERS":"OFFRES","HISTORY":"HISTORIQUE","What you receive":"Ce que vous recevez","WHAT YOU RECEIVE":"CE QUE VOUS RECEVEZ","This is shown before checkout so you know exactly what you are buying.":"Ceci est affiché avant le paiement pour savoir exactement ce que vous achetez.","Everything you need":"Tout ce dont vous avez besoin","Questions":"Questions","Subscription status":"Statut de l’abonnement","Order history":"Historique des commandes","Latest":"Dernières","Need help?":"Besoin d’aide ?","Expired":"Expiré","Mark all read":"Tout marquer comme lu","Mark read":"Marquer comme lu","No notifications.":"Aucune notification.","How can we help?":"Comment pouvons-nous vous aider ?","Subject":"Sujet","Related order (optional)":"Commande liée (optionnel)","Describe the issue...":"Décrivez le problème...","Your tickets":"Vos tickets","No tickets yet.":"Aucun ticket.","Write a reply...":"Écrire une réponse...","Send reply":"Envoyer la réponse","Future checkout methods":"Futurs modes de paiement","What customers see":"Ce que voient les clients","Select product":"Choisir un produit","Create a deal from existing plans":"Créer une offre avec les plans existants","Deal name":"Nom de l’offre","Bundle price":"Prix du bundle","Description":"Description","Create deal":"Créer l’offre","Scope existing coupons":"Limiter les coupons existants","All products":"Tous les produits","All categories":"Toutes les catégories"},
"sq":{"Available":"Në dispozicion","Unavailable":"Jo në dispozicion","products":"produkte","ACCOUNT":"LLOGARIA","SHOP":"DYQANI","OFFERS":"OFERTA","HISTORY":"HISTORIA","What you receive":"Çfarë merrni","WHAT YOU RECEIVE":"ÇFARË MERRNI","This is shown before checkout so you know exactly what you are buying.":"Kjo shfaqet para checkout-it që ta dini saktë çfarë po blini.","Everything you need":"Gjithçka që ju nevojitet","Questions":"Pyetje","Subscription status":"Statusi i abonimit","Order history":"Historia e porosive","Latest":"Të fundit","Need help?":"Keni nevojë për ndihmë?","Expired":"Skaduar","Mark all read":"Shëno të gjitha si të lexuara","Mark read":"Shëno si të lexuar","No notifications.":"Nuk ka njoftime.","How can we help?":"Si mund t'ju ndihmojmë?","Subject":"Subjekti","Related order (optional)":"Porosia përkatëse (opsionale)","Describe the issue...":"Përshkruani problemin...","Your tickets":"Ticket-at tuaja","No tickets yet.":"Ende nuk ka ticket-a.","Write a reply...":"Shkruaj përgjigje...","Send reply":"Dërgo përgjigjen","Future checkout methods":"Metodat e ardhshme të pagesës","What customers see":"Çfarë shohin klientët","Select product":"Zgjidh produktin","Create a deal from existing plans":"Krijo ofertë nga planet ekzistuese","Deal name":"Emri i ofertës","Bundle price":"Çmimi i bundle","Description":"Përshkrimi","Create deal":"Krijo ofertën","Scope existing coupons":"Kufizo kuponët ekzistues","All products":"Të gjitha produktet","All categories":"Të gjitha kategoritë"}}
for _l,_vals in V21_EXTRA.items(): V21_LANG[_l].update(_vals)

# Full V21/admin vocabulary. Keep source English keys stable so product data is never affected.
_V21_UI = {
    "en": {"Control center":"Control center","Manage your store faster":"Manage your store faster","Search products":"Search products","Collapse all":"Collapse all","Expand all":"Expand all","Product settings":"Product settings","Save product":"Save product","Delete permanently":"Delete permanently","Save plan":"Save plan","Delete plan":"Delete plan","Available":"Available","Unavailable":"Unavailable","What you receive":"What you receive","WHAT YOU RECEIVE":"WHAT YOU RECEIVE","Everything you need":"Everything you need","Questions":"Questions","Order history":"Order history","Need help?":"Need help?","Create a ticket if you need help.":"Create a ticket if you need help.","Manage future payment methods, product metadata, deals, coupon rules and support without touching the existing checkout flow.":"Manage future payment methods, product metadata, deals, coupon rules and support without touching the existing checkout flow.","Future checkout methods":"Future checkout methods","What customers see":"What customers see","Create a deal from existing plans":"Create a deal from existing plans","Scope existing coupons":"Scope existing coupons","Customer tickets":"Customer tickets","Reply to customer":"Reply to customer","Start":"Start","End":"End","Deal name":"Deal name","Description":"Description","Disable":"Disable","Enable":"Enable","Save":"Save","Reply":"Reply","pending":"pending","open":"open","resolved":"resolved","This is shown before checkout so you know exactly what you are buying.":"This is shown before checkout so you know exactly what you are buying.","How is delivery handled?":"How is delivery handled?","What type of access is this?":"What type of access is this?","Secure checkout":"Secure checkout","products ready to explore":"products ready to explore","Choose your service, compare plans and see exactly what you receive.":"Choose your service, compare plans and see exactly what you receive.","Bundle existing products and see your total savings before ordering.":"Bundle existing products and see your total savings before ordering.","Clear plans and prices.":"Clear plans and prices.","No hidden price before checkout.":"No hidden price before checkout.","Orders, subscriptions and wishlist.":"Orders, subscriptions and wishlist.","Tickets when you need help.":"Tickets when you need help."},
    "de": {"Control center":"Kontrollzentrum","Manage your store faster":"Verwalte deinen Shop schneller","Search products":"Produkte suchen","Collapse all":"Alle einklappen","Expand all":"Alle ausklappen","Product settings":"Produkteinstellungen","Save product":"Produkt speichern","Delete permanently":"Dauerhaft löschen","Save plan":"Plan speichern","Delete plan":"Plan löschen","Available":"Verfügbar","Unavailable":"Nicht verfügbar","What you receive":"Was du erhältst","WHAT YOU RECEIVE":"WAS DU ERHÄLTST","Everything you need":"Alles, was du brauchst","Questions":"Fragen","Order history":"Bestellverlauf","Need help?":"Brauchst du Hilfe?","Create a ticket if you need help.":"Erstelle ein Ticket, wenn du Hilfe brauchst.","Future checkout methods":"Zukünftige Zahlungsmethoden","What customers see":"Was Kunden sehen","Create a deal from existing plans":"Angebot aus bestehenden Plänen erstellen","Scope existing coupons":"Bestehende Gutscheine zuordnen","Customer tickets":"Kunden-Tickets","Reply to customer":"Kunde antworten","Deal name":"Angebotsname","Description":"Beschreibung","Start":"Start","End":"Ende","Reply":"Antworten","This is shown before checkout so you know exactly what you are buying.":"Dies wird vor dem Checkout angezeigt, damit du genau weißt, was du kaufst.","Secure checkout":"Sicherer Checkout","products ready to explore":"Produkte zum Entdecken","Choose your service, compare plans and see exactly what you receive.":"Wähle deinen Service, vergleiche Pläne und sieh genau, was du erhältst.","Clear plans and prices.":"Klare Pläne und Preise.","No hidden price before checkout.":"Keine versteckten Preise vor dem Checkout.","Orders, subscriptions and wishlist.":"Bestellungen, Abos und Wunschliste.","Tickets when you need help.":"Tickets, wenn du Hilfe brauchst."},
    "fr": {"Control center":"Centre de contrôle","Manage your store faster":"Gérez votre boutique plus rapidement","Search products":"Rechercher des produits","Collapse all":"Tout réduire","Expand all":"Tout développer","Product settings":"Paramètres du produit","Save product":"Enregistrer le produit","Delete permanently":"Supprimer définitivement","Save plan":"Enregistrer le plan","Delete plan":"Supprimer le plan","Available":"Disponible","Unavailable":"Indisponible","What you receive":"Ce que vous recevez","WHAT YOU RECEIVE":"CE QUE VOUS RECEVEZ","Everything you need":"Tout ce dont vous avez besoin","Questions":"Questions","Order history":"Historique des commandes","Need help?":"Besoin d'aide ?","Create a ticket if you need help.":"Créez un ticket si vous avez besoin d'aide.","Future checkout methods":"Futurs moyens de paiement","What customers see":"Ce que voient les clients","Create a deal from existing plans":"Créer une offre à partir des plans existants","Scope existing coupons":"Appliquer les coupons existants","Customer tickets":"Tickets clients","Reply to customer":"Répondre au client","Deal name":"Nom de l'offre","Description":"Description","Start":"Début","End":"Fin","Reply":"Répondre","This is shown before checkout so you know exactly what you are buying.":"Ceci est affiché avant le paiement pour que vous sachiez exactement ce que vous achetez.","Secure checkout":"Paiement sécurisé","products ready to explore":"produits à découvrir","Choose your service, compare plans and see exactly what you receive.":"Choisissez votre service, comparez les plans et voyez exactement ce que vous recevez.","Clear plans and prices.":"Plans et prix clairs.","No hidden price before checkout.":"Aucun prix caché avant le paiement.","Orders, subscriptions and wishlist.":"Commandes, abonnements et favoris.","Tickets when you need help.":"Tickets lorsque vous avez besoin d'aide."},
    "sq": {"Control center":"Qendra e kontrollit","Manage your store faster":"Menaxho dyqanin më shpejt","Search products":"Kërko produkte","Collapse all":"Mbyll të gjitha","Expand all":"Hap të gjitha","Product settings":"Cilësimet e produktit","Save product":"Ruaj produktin","Delete permanently":"Fshije përgjithmonë","Save plan":"Ruaj planin","Delete plan":"Fshi planin","Available":"Në dispozicion","Unavailable":"Nuk është në dispozicion","What you receive":"Çfarë merr","WHAT YOU RECEIVE":"ÇFARË MERR","Everything you need":"Gjithçka që të duhet","Questions":"Pyetje","Order history":"Historiku i porosive","Need help?":"Ke nevojë për ndihmë?","Create a ticket if you need help.":"Krijo një tiketë nëse ke nevojë për ndihmë.","Future checkout methods":"Metodat e ardhshme të pagesës","What customers see":"Çfarë shohin klientët","Create a deal from existing plans":"Krijo ofertë nga planet ekzistuese","Scope existing coupons":"Përcakto kuponët ekzistues","Customer tickets":"Tiketa të klientëve","Reply to customer":"Përgjigju klientit","Deal name":"Emri i ofertës","Description":"Përshkrimi","Start":"Fillimi","End":"Fundi","Reply":"Përgjigju","This is shown before checkout so you know exactly what you are buying.":"Kjo shfaqet para pagesës që ta dish saktë çfarë po blen.","Secure checkout":"Pagesë e sigurt","products ready to explore":"produkte për t'u parë","Choose your service, compare plans and see exactly what you receive.":"Zgjidh shërbimin, krahaso planet dhe shiko saktë çfarë merr.","Clear plans and prices.":"Plane dhe çmime të qarta.","No hidden price before checkout.":"Pa çmime të fshehura para pagesës.","Orders, subscriptions and wishlist.":"Porosi, abonime dhe lista e dëshirave.","Tickets when you need help.":"Tiketa kur të duhet ndihmë."}
}
for _l,_vals in _V21_UI.items(): V21_LANG[_l].update(_vals)

def v21_catalog_payload():
    """Complete catalog snapshot. Product data lives in DB; this is an explicit backup only."""
    out=[]
    for p in Product.query.order_by(Product.id.asc()).all():
        m=V21ProductMeta.query.filter_by(product_id=p.id).first()
        out.append({
            "id":p.id,"name":p.name,"slug":p.slug,"category_id":p.category_id,
            "category":p.category.name if p.category else "","active":bool(p.active),
            "featured":bool(p.featured),"sort_order":p.sort_order,"image_url":p.image_url or "",
            "card_label":p.card_label or "","price_label":p.price_label or "",
            "plans":[{"id":pl.id,"name":pl.name,"months":pl.months,"price":pl.price,
                       "sale_price":pl.sale_price,"cost_price":pl.cost_price,"active":bool(pl.active),
                       "sort_order":pl.sort_order} for pl in sorted(p.plans,key=lambda x:x.id)],
            "meta":({"product_type":m.product_type,"region":m.region,"delivery":m.delivery,
                     "guarantee":m.guarantee,"receive_type":m.receive_type,
                     "included_json":m.included_json,"faq_json":m.faq_json,
                     "badges_json":m.badges_json,"keywords":m.keywords,"desc_i18n":m.desc_i18n} if m else None)
        })
    return {"format":"veyra-catalog-v2","created_at":datetime.utcnow().isoformat(),"products":out}

def v21_save_catalog_backup(label="Automatic catalog snapshot", kind="snapshot"):
    payload=json.dumps(v21_catalog_payload(),ensure_ascii=False)
    b=V21CatalogBackup(label=label,kind=kind,payload=payload,created_by=(current_user.id if current_user.is_authenticated else None))
    db.session.add(b); db.session.flush()
    # Keep the backup table bounded; never delete the live catalog.
    old=V21CatalogBackup.query.order_by(V21CatalogBackup.created_at.desc()).offset(50).all()
    for x in old: db.session.delete(x)
    return b

def v21_tr(text):
    lang = getattr(request, "lang", None) or session.get("lang", "en")
    # V21 vocabulary first, then the complete store UI dictionary.
    return V21_LANG.get(lang, V21_LANG["en"]).get(text, TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(text, TRANSLATIONS["en"].get(text, text)))

def v21_json(value, default):
    try:
        return json.loads(value) if value else default
    except Exception:
        return default

def v21_meta(product):
    m = V21ProductMeta.query.filter_by(product_id=product.id).first()
    if not m:
        m = V21ProductMeta(product_id=product.id, product_type="Digital subscription", region="Worldwide", delivery="Fast digital delivery", guarantee="Support included", receive_type="Personal Account")
        db.session.add(m); db.session.commit()
    return m

def v21_billing(months):
    if months == 1: return "/month"
    if months == 3: return "/3 months"
    if months == 6: return "/6 months"
    if months == 12: return "/year"
    if months == 18: return "/18 months"
    if months <= 0: return "one-time"
    return f"/{months} months"

def v21_plan_price(plan):
    return float(plan.sale_price if plan.sale_price is not None else plan.price)

def v21_discount(plan):
    old = float(plan.price or 0); now = v21_plan_price(plan)
    return round(max(0, (old-now)/old*100)) if old else 0

def v21_badges(product, plan=None):
    m = v21_meta(product); badges = v21_json(m.badges_json, [])
    if product.featured and "POPULAR" not in badges: badges.append("POPULAR")
    if plan and v21_discount(plan) >= 20 and "BEST VALUE" not in badges: badges.append("BEST VALUE")
    return badges

def v21_expiry(sub):
    now = datetime.utcnow()
    if sub.expires_at <= now:
        sub.active = False; return 0, "expired"
    days = max(0, (sub.expires_at-now).days)
    return days, "soon" if days <= 7 else "active"

def v21_touch(product):
    key = session.get("v21_rv")
    if not key:
        key = secrets.token_urlsafe(24); session["v21_rv"] = key
    q = V21RecentlyViewed.query.filter_by(product_id=product.id)
    if current_user.is_authenticated: q = q.filter_by(user_id=current_user.id)
    else: q = q.filter_by(session_key=key, user_id=None)
    row = q.first()
    if row: row.viewed_at = datetime.utcnow()
    else: db.session.add(V21RecentlyViewed(product_id=product.id, user_id=current_user.id if current_user.is_authenticated else None, session_key=None if current_user.is_authenticated else key))
    db.session.commit()

def v21_recent():
    q = V21RecentlyViewed.query.order_by(V21RecentlyViewed.viewed_at.desc())
    if current_user.is_authenticated: q=q.filter_by(user_id=current_user.id)
    else: q=q.filter_by(session_key=session.get("v21_rv"), user_id=None)
    rows=q.limit(8).all(); seen=set(); out=[]
    for r in rows:
        if r.product_id not in seen and r.product and r.product.active: out.append(r.product); seen.add(r.product_id)
    return out

def v21_notify(user_id, title, body, kind="account"):
    db.session.add(V21Notification(user_id=user_id,title=title,body=body,kind=kind))

def v21_deal_active(d):
    now=datetime.utcnow()
    return d.active and (not d.starts_at or d.starts_at<=now) and (not d.ends_at or d.ends_at>=now)

def v21_payment_methods():
    defaults=[("card","Card"),("paypal","PayPal"),("stripe","Stripe"),("crypto","Crypto"),("other","Other")]
    rows=[]
    for i,(code,name) in enumerate(defaults):
        row=V21PaymentMethod.query.filter_by(code=code).first()
        if not row:
            row=V21PaymentMethod(code=code,name=name,enabled=False,sort_order=i); db.session.add(row)
        rows.append(row)
    db.session.commit(); return rows

@app.context_processor
def v21_context():
    unread = V21Notification.query.filter_by(user_id=current_user.id, read=False).count() if current_user.is_authenticated else 0
    return {"v21_tr": v21_tr, "v21_json": v21_json, "v21_billing": v21_billing, "v21_meta": v21_meta, "v21_discount": v21_discount, "v21_badges": v21_badges, "v21_unread": unread}

# New storefront endpoints are separate so legacy payment/order URLs remain untouched.
@app.route("/bundle/<int:bundle_id>")
def bundle_view(bundle_id):
    bundle=db.session.get(Bundle,bundle_id)
    if not bundle or not bundle.active:return ("Not found",404)
    items=[x for x in bundle.items if x.plan and x.plan.active and x.plan.product and x.plan.product.active]
    original=sum((x.plan.sale_price if x.plan.sale_price is not None else x.plan.price)*x.quantity for x in items)
    return render_template("v21_bundle.html",bundle=bundle,items=items,original=original)

@app.route("/manifest.webmanifest")
def manifest_webmanifest():
    return Response(json.dumps({"name":"VEYRA","short_name":"VEYRA","start_url":"/","display":"standalone","background_color":"#111a36","theme_color":"#111a36","description":"Digital marketplace","icons":[{"src":"/static/logos/VEYRA-logo-transparent.png","sizes":"512x512","type":"image/png"}]}),mimetype="application/manifest+json")

@app.route("/service-worker.js")
def service_worker():
    return Response("const CACHE='veyra-v1';self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/manifest.webmanifest']))));self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));});",mimetype="application/javascript")

@app.route("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /account\nDisallow: /v21/\n",mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap_xml():
    root=request.url_root.rstrip('/')
    urls=[root,root+'/categories']
    urls += [root+'/product/'+p.slug for p in Product.query.filter_by(active=True).all()]
    urls += [root+'/bundle/'+str(b.id) for b in Bundle.query.filter_by(active=True).all()]
    body='<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join('<url><loc>'+u+'</loc></url>' for u in urls)+'</urlset>'
    return Response(body,mimetype="application/xml")

@app.route("/v21/search")
def v21_search():
    q=request.args.get("q","").strip().lower()
    if not q: return {"results":[]}
    rows=[]
    for p in Product.query.filter_by(active=True).all():
        m=v21_meta(p); hay=f"{p.name} {p.category.name} {m.keywords}".lower()
        if q in hay or any(part.startswith(q) for part in re.split(r"[\s,;/|]+",hay) if part):
            rows.append({"name":p.name,"slug":p.slug,"category":p.category.name,"url":url_for("product",slug=p.slug)})
    return {"results":rows[:8]}

@app.route("/v21/wishlist/<int:product_id>", methods=["POST"])
@login_required
def v21_wishlist(product_id):
    p=db.session.get(Product,product_id)
    if not p:return ("Not found",404)
    row=Wishlist.query.filter_by(user_id=current_user.id,product_id=p.id).first()
    if row: db.session.delete(row); state=False
    else: db.session.add(Wishlist(user_id=current_user.id,product_id=p.id)); state=True
    db.session.commit()
    if request.headers.get("X-Requested-With")=="XMLHttpRequest": return {"ok":True,"saved":state}
    return redirect(request.referrer or url_for("product",slug=p.slug))

@app.route("/v21/notifications/read/<int:notification_id>", methods=["POST"])
@login_required
def v21_notification_read(notification_id):
    n=db.session.get(V21Notification,notification_id)
    if not n or n.user_id!=current_user.id:return ("Not found",404)
    n.read=True;db.session.commit();return redirect(url_for("v21_notifications"))

@app.route("/v21/notifications/read-all", methods=["POST"])
@login_required
def v21_notification_read_all():
    V21Notification.query.filter_by(user_id=current_user.id,read=False).update({"read":True});db.session.commit();return redirect(url_for("v21_notifications"))

@app.route("/v21/notifications")
@login_required
def v21_notifications():
    notes=V21Notification.query.filter_by(user_id=current_user.id).order_by(V21Notification.created_at.desc()).limit(100).all()
    return render_template("v21_notifications.html",notifications=notes)

@app.route("/v21/support", methods=["GET","POST"])
@login_required
def v21_support():
    if request.method=="POST":
        subject=request.form.get("subject","").strip() or "Support request"
        category=request.form.get("category","Other")
        msg=request.form.get("message","").strip()
        if not msg: flash("Please enter your message."); return redirect(url_for("v21_support"))
        t=V21Ticket(user_id=current_user.id,subject=subject,category=category,order_id=int(request.form["order_id"]) if request.form.get("order_id") else None,status="new")
        db.session.add(t);db.session.flush();db.session.add(V21TicketReply(ticket_id=t.id,user_id=current_user.id,message=msg,is_admin=False));db.session.commit()
        order_text = f"Order #{t.order_id}" if t.order_id else "No order linked"
        customer = f"@{current_user.username}"
        ticket_msg = (f"🎫 NEW VEYRA SUPPORT TICKET\n\nTicket: #{t.id}\nCustomer: {customer}\nCategory: {t.category}\nSubject: {t.subject}\n{order_text}\n\nMessage:\n{msg}")
        kb = {"inline_keyboard":[[{"text":"🎫 Open Ticket","url":request.url_root.rstrip("/")+url_for("admin_v21_ticket",ticket_id=t.id)}]]}
        send_telegram(ticket_msg, kb)
        return redirect(url_for("v21_ticket",ticket_id=t.id))
    tickets=V21Ticket.query.filter_by(user_id=current_user.id).order_by(V21Ticket.updated_at.desc()).all()
    return render_template("v21_support.html",tickets=tickets,orders=Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).limit(30).all())

@app.route("/v21/support/<int:ticket_id>", methods=["GET","POST"])
@login_required
def v21_ticket(ticket_id):
    t=db.session.get(V21Ticket,ticket_id)
    if not t or t.user_id!=current_user.id:return ("Not found",404)
    if request.method=="POST":
        msg=request.form.get("message","").strip()
        if msg: db.session.add(V21TicketReply(ticket_id=t.id,user_id=current_user.id,message=msg,is_admin=False));t.status="open";db.session.commit()
        return redirect(url_for("v21_ticket",ticket_id=t.id))
    return render_template("v21_ticket.html",ticket=t)

@app.route("/v21/coupon/check", methods=["POST"])
@login_required
def v21_coupon_check():
    code=request.form.get("coupon","").strip().upper(); plan_id=request.form.get("plan_id",type=int)
    c=Coupon.query.filter(db.func.upper(Coupon.code)==code,Coupon.active==True).first()
    if not c:return {"ok":False,"message":"Invalid coupon"}
    if c.expires_at and c.expires_at<datetime.utcnow():return {"ok":False,"message":"Coupon expired"}
    if c.max_uses and c.used_count>=c.max_uses:return {"ok":False,"message":"Coupon usage limit reached"}
    if CouponUsage.query.filter_by(coupon_id=c.id,user_id=current_user.id).first():return {"ok":False,"message":"Coupon already used"}
    if plan_id:
        plan=db.session.get(Plan,plan_id)
        rule=V21CouponRule.query.filter_by(coupon_id=c.id).first()
        if rule and rule.enabled and ((rule.product_id and (not plan or plan.product_id!=rule.product_id)) or (rule.category_id and (not plan or plan.product.category_id!=rule.category_id))):return {"ok":False,"message":"Coupon does not apply to this product"}
    return {"ok":True,"percent":c.discount_percent or 0,"fixed":c.discount_fixed or 0}

# v2.1 admin center
@app.route("/admin/v21", methods=["GET","POST"])
@login_required
def admin_v21():
    if not current_user.is_admin:return ("Forbidden",403)
    methods=v21_payment_methods()
    if request.method=="POST":
        action=request.form.get("action")
        if action=="payment":
            for m in methods:m.enabled=request.form.get(f"payment_{m.code}")=="on"
            db.session.commit();flash("Payment methods updated.")
        elif action=="meta":
            p=db.session.get(Product,request.form.get("product_id",type=int))
            if p:
                m=v21_meta(p);m.product_type=request.form.get("product_type","Digital subscription");m.region=request.form.get("region","Worldwide");m.delivery=request.form.get("delivery","Fast digital delivery");m.guarantee=request.form.get("guarantee","Support included");m.receive_type=request.form.get("receive_type","Personal Account");m.keywords=request.form.get("keywords","")
                m.included_json=json.dumps([x.strip() for x in request.form.get("included","").splitlines() if x.strip()]);m.badges_json=json.dumps(request.form.getlist("badges"));m.faq_json=json.dumps([{"q":x.split("|",1)[0].strip(),"a":x.split("|",1)[1].strip()} for x in request.form.get("faq","").splitlines() if "|" in x],ensure_ascii=False);db.session.commit();flash("Product metadata saved.")
        elif action=="deal":
            ids=[int(x) for x in request.form.getlist("plan_ids") if x.isdigit()];plans=[db.session.get(Plan,x) for x in ids];plans=[x for x in plans if x and x.active]
            if plans:
                price=float(request.form.get("deal_price",0) or 0)
                legacy=Bundle(name=request.form.get("deal_name","New Deal"),description=request.form.get("deal_description","") ,price=price,active=True,featured=request.form.get("featured")=="on")
                db.session.add(legacy);db.session.flush()
                for p in plans: db.session.add(BundleItem(bundle_id=legacy.id,plan_id=p.id,quantity=1))
                def _dt(name):
                    value=request.form.get(name,"").strip()
                    if not value:return None
                    try:return datetime.fromisoformat(value)
                    except ValueError:return None
                d=V21Deal(name=legacy.name,description=legacy.description,price=price,featured=legacy.featured,legacy_bundle_id=legacy.id,starts_at=_dt("starts_at"),ends_at=_dt("ends_at"))
                db.session.add(d);db.session.flush();[db.session.add(V21DealItem(deal_id=d.id,plan_id=p.id)) for p in plans];db.session.commit();flash("Deal created.")
        elif action=="deal_toggle":
            d=db.session.get(V21Deal,request.form.get("deal_id",type=int));
            if d:d.active=not d.active;db.session.commit()
        elif action=="coupon_rule":
            c=db.session.get(Coupon,request.form.get("coupon_id",type=int))
            if c:
                r=V21CouponRule.query.filter_by(coupon_id=c.id).first()
                if not r:r=V21CouponRule(coupon_id=c.id);db.session.add(r)
                r.product_id=request.form.get("product_id",type=int) or None
                r.category_id=request.form.get("category_id",type=int) or None
                r.enabled=request.form.get("enabled")=="on"
                db.session.commit();flash("Coupon rule saved.")
        return redirect(url_for("admin_v21"))
    products=Product.query.order_by(Product.sort_order,Product.name).all();deals=V21Deal.query.order_by(V21Deal.created_at.desc()).all();coupons=Coupon.query.order_by(Coupon.id.desc()).all();tickets=V21Ticket.query.order_by(V21Ticket.updated_at.desc()).limit(50).all();plans=Plan.query.filter_by(active=True).all()
    new_count=V21Ticket.query.filter_by(status="new").count()
    return render_template("v21_admin.html",methods=methods,products=products,deals=deals,coupons=coupons,tickets=tickets,plans=plans,new_count=new_count)

@app.route("/admin/catalog")
@login_required
def admin_catalog():
    if not current_user.is_admin:return ("Forbidden",403)
    products=Product.query.order_by(Product.sort_order.asc(),Product.name.asc()).all()
    return render_template("admin_catalog.html",products=products,backups=V21CatalogBackup.query.order_by(V21CatalogBackup.created_at.desc()).limit(25).all())

@app.route("/admin/catalog/export")
@login_required
def admin_catalog_export():
    if not current_user.is_admin:return ("Forbidden",403)
    products=[]
    for p in Product.query.order_by(Product.id.asc()).all():
        products.append({"id":p.id,"name":p.name,"slug":p.slug,"category":p.category.name if p.category else "","active":bool(p.active),"featured":bool(p.featured),"image_url":p.image_url or "","card_label":p.card_label or "","price_label":p.price_label or "","plans":[{"id":pl.id,"name":pl.name,"months":pl.months,"price":pl.price,"sale_price":pl.sale_price,"cost_price":pl.cost_price,"active":bool(pl.active)} for pl in sorted(p.plans,key=lambda x:x.id)]})
    from flask import Response
    return Response(json.dumps(products,ensure_ascii=False,indent=2),mimetype="application/json",headers={"Content-Disposition":"attachment; filename=veyra-product-catalog-backup.json"})

@app.route("/admin/v21/tickets")
@login_required
def admin_v21_tickets():
    if not current_user.is_admin:return ("Forbidden",403)
    status=request.args.get("status", "all").strip().lower()
    q=V21Ticket.query
    if status in {"new","open","waiting","pending","resolved","closed"}: q=q.filter_by(status=status)
    tickets=q.order_by(V21Ticket.updated_at.desc()).all()
    new_count=V21Ticket.query.filter_by(status="new").count()
    return render_template("v21_admin_tickets.html",tickets=tickets,new_count=new_count)

@app.route("/admin/v21/ticket/<int:ticket_id>", methods=["GET","POST"])
@login_required
def admin_v21_ticket(ticket_id):
    if not current_user.is_admin:return ("Forbidden",403)
    t=db.session.get(V21Ticket,ticket_id)
    if not t:return ("Not found",404)
    if request.method=="POST":
        msg=request.form.get("message","").strip()
        status=request.form.get("status",t.status)
        if msg:
            db.session.add(V21TicketReply(ticket_id=t.id,user_id=current_user.id,is_admin=True,message=msg))
            v21_notify(t.user_id,"Support update",f"Your ticket #{t.id} has a new reply.","support")
        if status in {"new","open","waiting","pending","resolved","closed"}: t.status=status
        db.session.commit()
        if msg:
            send_telegram(f"💬 REPLY SENT — Ticket #{t.id}\nCustomer: @{t.user.username}\nStatus: {t.status}\n\n{msg}")
        return redirect(url_for("admin_v21_ticket",ticket_id=t.id))
    if t.status=="new":
        t.status="pending"
        db.session.commit()
    return render_template("v21_admin_ticket.html",ticket=t)

@app.route("/admin/v21/ticket/<int:ticket_id>/reply", methods=["POST"])
@login_required
def admin_v21_ticket_reply(ticket_id):
    if not current_user.is_admin:return ("Forbidden",403)
    t=db.session.get(V21Ticket,ticket_id)
    if not t:return ("Not found",404)
    msg=request.form.get("message","").strip()
    if msg:
        db.session.add(V21TicketReply(ticket_id=t.id,user_id=current_user.id,is_admin=True,message=msg));t.status=request.form.get("status","pending");v21_notify(t.user_id,"Support update",f"Your ticket #{t.id} has a new reply.","support");db.session.commit()
    return redirect(url_for("admin_v21"))

# Use the v2.1 storefront/dashboard while preserving legacy route endpoints for checkout/payment.
def v21_index():
    products=Product.query.filter_by(active=True).order_by(Product.sort_order,Product.id).all()
    categories=Category.query.order_by(Category.name).all()
    bundles=Bundle.query.filter_by(active=True).order_by(Bundle.featured.desc(),Bundle.name).all()
    deals=[d for d in V21Deal.query.order_by(V21Deal.created_at.desc()).all() if v21_deal_active(d)]
    recent=v21_recent()
    return render_template("v21_index.html",products=products,categories=categories,deals=deals,bundles=bundles,recent=recent)

def v21_product(slug):
    p=Product.query.filter_by(slug=slug,active=True).first_or_404();v21_touch(p);reviews=Review.query.filter_by(product_id=p.id,approved=True).order_by(Review.created_at.desc()).all();meta=v21_meta(p);included=v21_json(meta.included_json,[]);faq=v21_json(meta.faq_json,[]);plans=sorted([x for x in p.plans if x.active],key=lambda x:x.sort_order);saved=current_user.is_authenticated and Wishlist.query.filter_by(user_id=current_user.id,product_id=p.id).first() is not None
    return render_template("v21_product.html",product=p,meta=meta,included=included,faq=faq,plans=plans,reviews=reviews,saved=saved,recent=v21_recent())

def v21_account():
    subs=Subscription.query.filter_by(user_id=current_user.id).order_by(Subscription.expires_at.asc()).all();changed=False
    for s in subs:
        old=s.active;s.active=(s.expires_at>datetime.utcnow())
        if old and not s.active:v21_notify(current_user.id,"Subscription expired",f"{s.product.name} has expired.","subscription");changed=True
        elif s.active and (s.expires_at-datetime.utcnow()).days<=7 and old:
            exists=V21Notification.query.filter_by(user_id=current_user.id,kind="expiration").filter(V21Notification.body.like(f"%{s.product.name}%")).first()
            if not exists:v21_notify(current_user.id,"Subscription expiring soon",f"{s.product.name} expires on {s.expires_at.strftime('%d.%m.%Y')}.","expiration");changed=True
    if changed:db.session.commit()
    orders=Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    for o in orders[:20]:
        label = o.plan.product.name if o.plan else (o.bundle.name if o.bundle else "Order")
        if not V21Notification.query.filter_by(user_id=current_user.id,kind="order").filter(V21Notification.body.like(f"%{public_order_ref(o)}%")).first():
            v21_notify(current_user.id,"Order update",f"{public_order_ref(o)} · {label} · {o.status}","order")
        if o.delivery and not V21Notification.query.filter_by(user_id=current_user.id,kind="delivery").filter(V21Notification.body.like(f"%{public_order_ref(o)}%")).first():
            v21_notify(current_user.id,"Delivery ready",f"{public_order_ref(o)} is ready in your account.","delivery")
    active_deals=V21Deal.query.filter_by(active=True).count()
    if active_deals and not V21Notification.query.filter_by(user_id=current_user.id,kind="deal").first():
        v21_notify(current_user.id,"New deals available",f"There are {active_deals} active deals to explore.","deal")
    db.session.commit()
    wishlist=Wishlist.query.filter_by(user_id=current_user.id).all();tickets=V21Ticket.query.filter_by(user_id=current_user.id).order_by(V21Ticket.updated_at.desc()).limit(5).all();notes=V21Notification.query.filter_by(user_id=current_user.id).order_by(V21Notification.created_at.desc()).limit(6).all();points=sum(x.points for x in LoyaltyPoint.query.filter_by(user_id=current_user.id).all())
    return render_template("v21_account.html",subs=subs,orders=orders,wishlist=wishlist,tickets=tickets,notifications=notes,points=points,recent=v21_recent())

def v21_wishlist_page():
    items=Wishlist.query.filter_by(user_id=current_user.id).order_by(Wishlist.id.desc()).all();return render_template("v21_wishlist.html",items=items)

app.view_functions["index"]=v21_index
app.view_functions["product"]=v21_product
app.view_functions["account"]=login_required(v21_account)
app.view_functions["wishlist_page"]=login_required(v21_wishlist_page)

with app.app_context():
    db.create_all()
    v21_payment_methods()
    # Sensible metadata defaults for the current catalog. Gemini keeps the requested 18-month / 5 TB offer copy.
    for _p in Product.query.all():
        _m = V21ProductMeta.query.filter_by(product_id=_p.id).first()
        if not _m:
            _m = V21ProductMeta(product_id=_p.id)
            db.session.add(_m)
        if not _m.keywords:
            _m.keywords = f"{_p.name} {_p.category.name}"
        if _p.slug == "gemini":
            _m.product_type = "AI subscription"
            _m.region = "Worldwide"
            _m.receive_type = "Personal Account"
            _m.delivery = "Activated directly on your personal Google account"
            _m.guarantee = "Support included"
            _m.included_json = json.dumps(["18 months of Gemini Premium", "5 TB Google Drive storage", "Activated directly on your personal Google account"], ensure_ascii=False)
            _m.desc_i18n = json.dumps({"en":"18 months of Gemini Premium + 5 TB Google Drive storage, activated directly on your personal Google account."}, ensure_ascii=False)
            _m.keywords = "gemini google ai ai pro premium google drive 5tb"
    db.session.commit()


# ---------------- V21 Admin Control Center v2 ----------------
def _admin_only():
    return current_user.is_authenticated and current_user.is_admin

@app.route("/admin/catalog/backup", methods=["POST"])
@login_required
def admin_catalog_backup():
    if not _admin_only(): return ("Forbidden",403)
    b=v21_save_catalog_backup("Manual catalog backup", "manual")
    db.session.commit()
    flash(f"Catalog backup #{b.id} created.")
    return redirect(url_for("admin_catalog"))

@app.route("/admin/catalog/export.csv")
@login_required
def admin_catalog_export_csv():
    if not _admin_only(): return ("Forbidden",403)
    output=io.StringIO(); w=csv.writer(output)
    w.writerow(["product_id","product","slug","category","active","featured","plan_id","plan","months","price","sale_price","cost_price","plan_active","plan_sort_order"])
    for p in Product.query.order_by(Product.id.asc()).all():
        for pl in sorted(p.plans,key=lambda x:x.id) or [None]:
            w.writerow([p.id,p.name,p.slug,p.category.name if p.category else "",int(bool(p.active)),int(bool(p.featured)),
                        pl.id if pl else "",pl.name if pl else "",pl.months if pl else "",pl.price if pl else "",
                        pl.sale_price if pl else "",pl.cost_price if pl else "",int(bool(pl.active)) if pl else "",pl.sort_order if pl else ""])
    return Response(output.getvalue(),mimetype="text/csv",headers={"Content-Disposition":"attachment; filename=veyra-catalog.csv"})

@app.route("/admin/catalog/restore", methods=["POST"])
@login_required
def admin_catalog_restore():
    if not _admin_only(): return ("Forbidden",403)
    f=request.files.get("backup")
    if not f or not f.filename.lower().endswith(".json"):
        flash("Upload a Veyra JSON catalog backup."); return redirect(url_for("admin_catalog"))

    def _normalise_catalog_json(raw):
        """Accept both the current wrapper format and the old raw-list format."""
        if isinstance(raw, list):
            products = raw
        elif isinstance(raw, dict):
            products = raw.get("products")
            if products is None and isinstance(raw.get("catalog"), dict):
                products = raw["catalog"].get("products")
        else:
            products = None
        if not isinstance(products, list):
            raise ValueError('Invalid catalog format. Expected a Veyra backup object with a "products" array, or a product array [...].')
        clean=[]
        for item in products:
            if not isinstance(item, dict) or not (item.get("id") or item.get("slug") or item.get("name")):
                continue
            plans=item.get("plans")
            if not isinstance(plans, list): plans=[]
            item=dict(item); item["plans"]=[x for x in plans if isinstance(x,dict)]
            clean.append(item)
        if not clean and products:
            raise ValueError("Catalog contains no valid products.")
        return clean

    def _delete_orders_for_plan_ids(plan_ids):
        if not plan_ids: return
        order_ids=[x.id for x in Order.query.filter(Order.plan_id.in_(plan_ids)).all()]
        if order_ids:
            # Tickets keep their conversation, but must not retain a deleted order FK.
            if "V21Ticket" in globals():
                V21Ticket.query.filter(V21Ticket.order_id.in_(order_ids)).update({V21Ticket.order_id: None}, synchronize_session=False)
            OrderDelivery.query.filter(OrderDelivery.order_id.in_(order_ids)).delete(synchronize_session=False)
            Subscription.query.filter(Subscription.order_id.in_(order_ids)).delete(synchronize_session=False)
            CouponUsage.query.filter(CouponUsage.order_id.in_(order_ids)).delete(synchronize_session=False)
            Order.query.filter(Order.id.in_(order_ids)).delete(synchronize_session=False)

    def _delete_plan_ids(plan_ids):
        if not plan_ids: return
        _delete_orders_for_plan_ids(plan_ids)
        if "V21DealItem" in globals():
            deal_ids=[x.deal_id for x in V21DealItem.query.filter(V21DealItem.plan_id.in_(plan_ids)).all()]
            V21DealItem.query.filter(V21DealItem.plan_id.in_(plan_ids)).delete(synchronize_session=False)
            for did in set(deal_ids):
                d=db.session.get(V21Deal,did) if "V21Deal" in globals() else None
                if d and not V21DealItem.query.filter_by(deal_id=did).first():
                    db.session.delete(d)
        BundleItem.query.filter(BundleItem.plan_id.in_(plan_ids)).delete(synchronize_session=False)
        Subscription.query.filter(Subscription.plan_id.in_(plan_ids)).delete(synchronize_session=False)
        db.session.execute(db.delete(Plan).where(Plan.id.in_(plan_ids)))

    def _delete_product_for_restore(prod):
        plan_ids=[x.id for x in prod.plans]
        _delete_plan_ids(plan_ids)
        Review.query.filter_by(product_id=prod.id).delete(synchronize_session=False)
        Wishlist.query.filter_by(product_id=prod.id).delete(synchronize_session=False)
        if "V21ProductMeta" in globals(): V21ProductMeta.query.filter_by(product_id=prod.id).delete(synchronize_session=False)
        if "V21RecentlyViewed" in globals(): V21RecentlyViewed.query.filter_by(product_id=prod.id).delete(synchronize_session=False)
        if "V21CouponRule" in globals(): V21CouponRule.query.filter_by(product_id=prod.id).delete(synchronize_session=False)
        db.session.delete(prod)

    try:
        raw=json.loads(f.read().decode("utf-8-sig"))
        products=_normalise_catalog_json(raw)

        # Always snapshot the LIVE state before a restore. This makes the restore reversible.
        before=v21_save_catalog_backup("Before restore", "pre-restore")

        backup_ids={int(x["id"]) for x in products if str(x.get("id","")).isdigit()}
        backup_slugs={str(x.get("slug")).strip().lower() for x in products if x.get("slug")}
        current=Product.query.order_by(Product.id.asc()).all()
        removed=[]; updated=0; added=0; plans_added=0; plans_removed=0

        # IMPORTANT: restore means the catalog becomes the backup catalog.
        # Products that exist NOW but are absent from the backup are removed.
        # Orders/customers are not touched unless a removed product/plan has dependent orders;
        # in that case the same safe dependency cleanup used by permanent product deletion runs.
        for p in current:
            if p.id not in backup_ids and (p.slug or "").strip().lower() not in backup_slugs:
                removed.append(p.name)
                _delete_product_for_restore(p)
        db.session.flush()

        for item in products:
            pid=item.get("id")
            p=db.session.get(Product, int(pid)) if str(pid).isdigit() else None
            slug=(item.get("slug") or make_slug(item.get("name","product"))).strip()
            if not p:
                p=Product.query.filter_by(slug=slug).first()
            if not p:
                cat=db.session.get(Category,item.get("category_id"))
                if not cat and item.get("category"):
                    cat=Category.query.filter_by(name=item["category"]).first()
                if not cat:
                    raise ValueError(f'Category not found for product "{item.get("name","Product")}"')
                p=Product(name=item.get("name") or "Product",slug=slug,category_id=cat.id)
                db.session.add(p); db.session.flush(); added+=1
            old=(p.name,p.slug,p.category_id,p.active,p.featured,p.sort_order,p.image_url,p.card_label,p.price_label)
            p.name=item.get("name",p.name); p.slug=slug
            if item.get("category_id") is not None:
                cat=db.session.get(Category,int(item["category_id"]))
                if cat: p.category_id=cat.id
            elif item.get("category"):
                cat=Category.query.filter_by(name=item["category"]).first()
                if cat: p.category_id=cat.id
            p.active=bool(item.get("active",p.active)); p.featured=bool(item.get("featured",p.featured))
            p.sort_order=int(item.get("sort_order",p.sort_order or 0)); p.image_url=item.get("image_url",p.image_url) or ""
            p.card_label=item.get("card_label",p.card_label) or ""; p.price_label=item.get("price_label",p.price_label) or "month"
            if (p.name,p.slug,p.category_id,p.active,p.featured,p.sort_order,p.image_url,p.card_label,p.price_label)!=old: updated+=1

            restored_plan_ids=set()
            for pi in item.get("plans",[]):
                if pi.get("id") is None: continue
                try: plid=int(pi["id"])
                except (TypeError,ValueError): continue
                pl=db.session.get(Plan,plid)
                # Never attach a plan belonging to another product to this product.
                if pl and pl.product_id != p.id:
                    pl=None
                if not pl:
                    pl=Plan.query.filter_by(product_id=p.id,name=pi.get("name") or "Plan").first()
                if not pl:
                    pl=Plan(product_id=p.id,name=pi.get("name") or "Plan",months=int(pi.get("months",1) or 1),price=float(pi.get("price",0) or 0),sale_price=pi.get("sale_price"),cost_price=float(pi.get("cost_price",0) or 0),active=bool(pi.get("active",True)),sort_order=int(pi.get("sort_order",0) or 0))
                    db.session.add(pl); db.session.flush(); plans_added+=1
                else:
                    pl.product_id=p.id
                    pl.name=pi.get("name",pl.name); pl.months=int(pi.get("months",pl.months) or 1); pl.price=float(pi.get("price",pl.price) or 0)
                    pl.sale_price=pi.get("sale_price",pl.sale_price); pl.cost_price=float(pi.get("cost_price",pl.cost_price or 0) or 0)
                    pl.active=bool(pi.get("active",pl.active)); pl.sort_order=int(pi.get("sort_order",pl.sort_order) or 0)
                if pl.id is not None:
                    restored_plan_ids.add(pl.id)

            # Remove plans that were added after this backup and are not in the backup.
            stale=[pl.id for pl in list(p.plans) if pl.id not in restored_plan_ids and pl.id is not None]
            if stale:
                plans_removed += len(stale)
                _delete_plan_ids(stale)

            meta=item.get("meta")
            if meta:
                m=V21ProductMeta.query.filter_by(product_id=p.id).first()
                if not m: m=V21ProductMeta(product_id=p.id); db.session.add(m)
                for k in ["product_type","region","delivery","guarantee","receive_type","included_json","faq_json","badges_json","keywords","desc_i18n"]:
                    if k in meta: setattr(m,k,meta[k])

        db.session.commit()
        audit("Catalog restored", f"Exact catalog rollback: +{added} products, removed {len(removed)} products, updated {updated} products, +{plans_added} plans, removed {plans_removed} plans")
        db.session.commit()
        removed_txt = ", ".join(removed[:5]) + ("…" if len(removed)>5 else "")
        extra = f" Removed: {removed_txt}." if removed else ""
        flash(f"Catalog restored successfully. Added {added}, updated {updated}, removed {len(removed)} products, removed {plans_removed} plans.{extra} Pre-restore backup #{before.id} created.")
    except Exception as e:
        db.session.rollback()
        flash(f"Restore failed: {e}")
    return redirect(url_for("admin_catalog"))

@app.route("/admin/catalog/backup/<int:backup_id>/download")
@login_required
def admin_catalog_backup_download(backup_id):
    if not _admin_only(): return ("Forbidden",403)
    b=db.session.get(V21CatalogBackup,backup_id)
    if not b:return ("Not found",404)
    return Response(b.payload,mimetype="application/json",headers={"Content-Disposition":f"attachment; filename=veyra-catalog-backup-{b.id}.json"})

@app.route("/admin/catalog/compare")
@login_required
def admin_catalog_compare():
    if not current_user.is_admin:return ("Forbidden",403)
    backups=V21CatalogBackup.query.order_by(V21CatalogBackup.created_at.desc()).limit(8).all()
    current={p.id:p for p in Product.query.all()}
    rows=[]
    for b in backups:
        try: data=json.loads(b.payload); products=data.get("products",data) if isinstance(data,dict) else data
        except Exception: products=[]
        ids={int(x.get("id")) for x in products if isinstance(x,dict) and str(x.get("id","")).isdigit()}
        slugs={str(x.get("slug")) for x in products if isinstance(x,dict) and x.get("slug")}
        current_slugs={p.slug for p in current.values()}
        added=[p for p in current.values() if p.id not in ids and p.slug not in slugs]
        missing=[x for x in products if isinstance(x,dict) and not ((str(x.get("id","")).isdigit() and int(x.get("id")) in current) or (x.get("slug") in current_slugs))]
        changed_plans=[]
        for x in products:
            if not isinstance(x,dict): continue
            cp=current.get(int(x.get("id"))) if str(x.get("id","")).isdigit() else None
            if not cp and x.get("slug"): cp=Product.query.filter_by(slug=x.get("slug")).first()
            if not cp: continue
            current_plans={(pl.id,pl.name):pl for pl in cp.plans}
            backup_plans=x.get("plans") or []
            for bp in backup_plans:
                bp_id=int(bp.get("id")) if str(bp.get("id","")).isdigit() else None
                pl=current_plans.get((bp_id,bp.get("name"))) if bp_id else None
                if not pl:
                    pl=next((v for (pid,n),v in current_plans.items() if n==bp.get("name")),None)
                if not pl: continue
                diffs=[]
                for field in ("months","price","sale_price","cost_price","active"):
                    bv=bp.get(field); cv=getattr(pl,field)
                    if isinstance(bv,(int,float)) and isinstance(cv,(int,float)):
                        if abs(float(bv)-float(cv))>0.0001: diffs.append(f"{field}: {bv} → {cv}")
                    elif bv != cv: diffs.append(f"{field}: {bv} → {cv}")
                if diffs: changed_plans.append({"product":cp.name,"plan":pl.name,"diffs":diffs})
        rows.append({"backup":b,"backup_products":len(products),"current_products":len(current),"current_only":added,"backup_only":missing,"changed_plans":changed_plans})
    return render_template("admin_catalog_compare.html",rows=rows)

@app.route("/admin/v21/ticket/<int:ticket_id>/status", methods=["POST"])
@login_required
def admin_v21_ticket_status(ticket_id):
    if not _admin_only(): return ("Forbidden",403)
    t=db.session.get(V21Ticket,ticket_id)
    if not t:return ("Not found",404)
    status=request.form.get("status","")
    if status not in {"new","open","waiting","pending","resolved","closed"}: return ("Bad status",400)
    t.status=status; db.session.commit()
    v21_notify(t.user_id,"Support ticket update",f"Ticket #{t.id} is now {status}.","support"); db.session.commit()
    return redirect(url_for("admin_v21_ticket",ticket_id=t.id))

