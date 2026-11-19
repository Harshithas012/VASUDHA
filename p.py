import os
import uuid
import smtplib

from datetime import datetime, timezone
from email.message import EmailMessage

import firebase_admin
from firebase_admin import credentials, firestore

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for
)

from dotenv import load_dotenv


# -----------------------------------------
# ENVIRONMENT CONFIGURATION
# -----------------------------------------

load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

APP_BASE_URL = os.getenv(
    "APP_BASE_URL",
    "http://127.0.0.1:5000"
).rstrip("/")


# -----------------------------------------
# FIREBASE CONFIGURATION
# -----------------------------------------

db = None

try:
    if not firebase_admin._apps:

        service_account_path = os.getenv(
            "FIREBASE_SERVICE_ACCOUNT",
            "serviceAccountKey.json"
        )

        if os.path.exists(service_account_path):
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
        else:
            raise FileNotFoundError(
                "Firebase service account file not found."
            )

    db = firestore.client()

    print("Firebase connected successfully.")

except Exception as error:
    print("Firebase connection error:", error)


# -----------------------------------------
# GENERAL HELPERS
# -----------------------------------------

def response_error(message, status=400):
    return jsonify({"error": message}), status


def current_user():
    return session.get("user")


def require_login():
    user = current_user()

    if not user:
        return None, response_error("Please login first.", 401)

    return user, None


def serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            key: serialize(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [serialize(item) for item in value]

    return value


def get_database():
    if db is None:
        raise RuntimeError(
            "Firebase is not connected. Check your service account file."
        )

    return db


def get_listing_document(listing_id):
    database = get_database()

    document = database.collection("listings").document(
        listing_id
    ).get()

    if not document.exists:
        return None, None

    return document, document.to_dict()


def get_request_document(request_id):
    database = get_database()

    document = database.collection("requests").document(
        request_id
    ).get()

    if not document.exists:
        return None, None

    return document, document.to_dict()


# -----------------------------------------
# EMAIL CONFIGURATION
# -----------------------------------------

def send_email(to_email, subject, body):
    smtp_email = os.getenv("EMAIL_ADDRESS")
    smtp_password = os.getenv("EMAIL_PASSWORD")

    if not smtp_email or not smtp_password:
        print("Email settings are missing.")
        return False

    if not to_email:
        return False

    try:
        message = EmailMessage()

        message["Subject"] = subject
        message["From"] = smtp_email
        message["To"] = to_email

        message.set_content(body)

        with smtplib.SMTP(
            "smtp.gmail.com",
            587,
            timeout=20
        ) as server:

            server.starttls()

            server.login(
                smtp_email,
                smtp_password
            )

            server.send_message(message)

        return True

    except Exception as error:
        print("Email sending error:", error)
        return False


# -----------------------------------------
# HOME PAGE
# -----------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


# -----------------------------------------

# -----------------------------------------

@app.route("/api/demo-login", methods=["POST"])
def demo_login():

    data = request.get_json(silent=True) or {}

    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or "@" not in email:
        return response_error("Enter a valid email address.")

    if len(password) < 6:
        return response_error(
            "Password must contain at least 6 characters."
        )

    if not name:
        name = email.split("@")[0]

    user = {
        "id": email,
        "name": name,
        "email": email
    }

    session["user"] = user

    return jsonify({
        "success": True,
        "user": user
    })


# -----------------------------------------
# CURRENT USER
# -----------------------------------------

@app.route("/api/me", methods=["GET"])
def get_me():

    user = current_user()

    return jsonify({
        "user": user
    })


# -----------------------------------------
# LOGOUT
# -----------------------------------------

@app.route("/api/logout", methods=["POST"])
def logout():

    session.clear()

    return jsonify({
        "success": True,
        "message": "Logged out successfully."
    })


# -----------------------------------------
# LISTINGS: GET AND PUBLISH
# -----------------------------------------

@app.route("/api/listings", methods=["GET", "POST"])
def listings():

    user, error = require_login()

    if error:
        return error

    database = get_database()

    # GET LISTINGS

    if request.method == "GET":

        district = request.args.get(
            "district", ""
        ).strip().lower()

        place = request.args.get(
            "place", ""
        ).strip().lower()

        category = request.args.get(
            "category", ""
        ).strip().lower()

        results = []

        documents = database.collection(
            "listings"
        ).stream()

        for document in documents:

            item = document.to_dict()
            item["id"] = document.id

            if item.get("status") != "Available":
                continue

            if category and item.get("category", "").lower() != category:
                continue

            if district and item.get("district", "").lower() != district:
                continue

            if place and place not in item.get("village", "").lower():
                continue

            # Never expose owner's private email or phone.
            item.pop("owner_email", None)
            item.pop("owner_phone", None)

            item.pop("exact_lat", None)
            item.pop("exact_lng", None)
            item.pop("latitude", None)
            item.pop("longitude", None)

            results.append(serialize(item))

        return jsonify({
            "listings": results
        })

    # PUBLISH LISTING

    data = request.get_json(silent=True) or {}

    category = data.get("category", "").strip().lower()

    if category not in ["land", "equipment"]:
        return response_error("Invalid listing category.")

    title = data.get("title", "").strip()

    district = data.get("district", "").strip()

    village = data.get("village", "").strip()

    if not title:
        return response_error("Enter a listing title.")

    if not district:
        return response_error("Enter a district.")

    item = {
        "category": category,
        "title": title,
        "district": district,
        "village": village,

        "owner_id": user["id"],
        "owner_name": user["name"],
        "owner_email": user["email"],

        "created_at": datetime.now(timezone.utc),

        "status": "Available",
        "details": data.get("details", "").strip(),
        "availability": data.get(
            "availability",
            "Available"
        )
    }

    # LAND DETAILS

    if category == "land":

        try:
            acres = float(data.get("acres", 0))
            rate = float(data.get("price_per_acre_lakh", 0))
            total_crore = float(
                data.get("total_price_crore", 0)
            )

            if acres <= 0 or rate < 0 or total_crore < 0:
                return response_error("Enter valid land details.")

        except (TypeError, ValueError):
            return response_error("Invalid land price or area.")

        item.update({
            "acres": acres,
            "land_type": data.get("land_type", ""),
            "soil_type": data.get("soil_type", ""),
            "water": data.get("water", ""),
            "road_access": data.get("road_access", ""),
            "price_per_acre_lakh": rate,
            "asking_total_crore": total_crore
        })

    # EQUIPMENT DETAILS

    if category == "equipment":

        try:
            rental_price = float(
                data.get("rental_price", 0)
            )

            if rental_price < 0:
                return response_error("Invalid equipment price.")

        except (TypeError, ValueError):
            return response_error("Invalid equipment price.")

        item.update({
            "equipment_type": data.get(
                "equipment_type", ""
            ),
            "rental_price": rental_price,
            "exchange_for": data.get(
                "exchange_for", ""
            ).strip()
        })

    document_ref = database.collection(
        "listings"
    ).document()

    document_ref.set(item)

    return jsonify({
        "success": True,
        "message": "Listing published successfully.",
        "listing_id": document_ref.id
    }), 201


# -----------------------------------------
# DELETE LISTING
# ONLY PUBLISHER CAN DELETE
# -----------------------------------------

@app.route(
    "/api/listings/<listing_id>",
    methods=["DELETE"]
)
def delete_listing(listing_id):

    user, error = require_login()

    if error:
        return error

    document, item = get_listing_document(listing_id)

    if not item:
        return response_error("Listing not found.", 404)

    if item.get("owner_id") != user["id"]:
        return response_error(
            "You can delete only your own listing.",
            403
        )

    document.reference.delete()

    return jsonify({
        "success": True,
        "message": "Listing deleted successfully."
    })


# -----------------------------------------
# SEND AND VIEW REQUESTS
# -----------------------------------------

@app.route("/api/requests", methods=["GET", "POST"])
def requests_api():

    user, error = require_login()

    if error:
        return error

    database = get_database()

    # GET REQUESTS

    if request.method == "GET":

        sent_requests = []
        received_requests = []

        documents = database.collection(
            "requests"
        ).stream()

        for document in documents:

            item = document.to_dict()
            item["id"] = document.id

            # Keep private email information server-side.
            item.pop("owner_email", None)
            item.pop("buyer_email", None)
            item.pop("buyer_phone", None)

            item = serialize(item)

            if item.get("buyer_id") == user["id"]:
                sent_requests.append(item)

            if item.get("owner_id") == user["id"]:
                received_requests.append(item)

        return jsonify({
            "sent_requests": sent_requests,
            "received_requests": received_requests
        })

    # CREATE REQUEST

    data = request.get_json(silent=True) or {}

    listing_id = data.get("listing_id", "").strip()

    message = data.get("message", "").strip()

    if not listing_id:
        return response_error("Listing ID is required.")

    if not message:
        return response_error("Enter a request message.")

    document, listing = get_listing_document(listing_id)

    if not listing:
        return response_error("Listing not found.", 404)

    if listing.get("status") != "Available":
        return response_error("This listing is no longer available.")

    owner_id = listing.get("owner_id")

    if owner_id == user["id"]:
        return response_error(
            "You cannot send a request to your own listing."
        )

    owner_email = listing.get("owner_email")

    request_id = str(uuid.uuid4())

    request_data = {
        "listing_id": listing_id,
        "listing_title": listing.get("title", ""),
        "category": listing.get("category", ""),

        "buyer_id": user["id"],
        "buyer_name": user["name"],
        "buyer_email": user["email"],
        "buyer_phone": data.get("buyer_phone", "").strip(),

        "owner_id": owner_id,
        "owner_name": listing.get("owner_name", ""),
        "owner_email": owner_email,

        "message": message,
        "period": data.get("period", ""),

        "status": "Pending",

        "created_at": datetime.now(timezone.utc)
    }

    database.collection(
        "requests"
    ).document(request_id).set(request_data)

    # EMAIL NOTIFICATION TO OWNER

    chat_link = (
        APP_BASE_URL +
        "/?request_id=" +
        request_id
    )

    email_body = f"""
Hello {listing.get('owner_name', 'User')},

You have received a new request on VASUDHA.

Listing: {listing.get('title', '')}

Requester: {user['name']}

Message:
{message}

Login to VASUDHA and open your received requests.

Private conversation link:
{chat_link}

Request ID:
{request_id}

Thank you,
VASUDHA Smart Agriculture Platform
"""

    email_sent = send_email(
        owner_email,
        "New Land / Equipment Request - VASUDHA",
        email_body
    )

    return jsonify({
        "success": True,
        "message": "Request sent successfully.",
        "request_id": request_id,
        "email_sent": email_sent
    }), 201


# -----------------------------------------
# GET ONE REQUEST
# ONLY REQUEST PARTICIPANTS
# -----------------------------------------

@app.route(
    "/api/requests/<request_id>",
    methods=["GET"]
)
def get_one_request(request_id):

    user, error = require_login()

    if error:
        return error

    document, item = get_request_document(request_id)

    if not item:
        return response_error("Request not found.", 404)

    if user["id"] not in [
        item.get("buyer_id"),
        item.get("owner_id")
    ]:
        return response_error(
            "You are not allowed to view this request.",
            403
        )

    item["id"] = request_id

    item.pop("owner_email", None)
    item.pop("buyer_email", None)
    item.pop("buyer_phone", None)

    return jsonify({
        "request": serialize(item)
    })


# -----------------------------------------
# ACCEPT OR REJECT REQUEST
# -----------------------------------------

@app.route(
    "/api/requests/<request_id>/status",
    methods=["POST"]
)
def update_request_status(request_id):

    user, error = require_login()

    if error:
        return error

    data = request.get_json(silent=True) or {}

    new_status = data.get("status", "").strip()

    if new_status not in ["Accepted", "Rejected"]:
        return response_error("Invalid request status.")

    document, item = get_request_document(request_id)

    if not item:
        return response_error("Request not found.", 404)

    # Only the listing owner can accept or reject.
    if item.get("owner_id") != user["id"]:
        return response_error(
            "Only the listing owner can update this request.",
            403
        )

    if item.get("status") != "Pending":
        return response_error(
            "This request has already been processed."
        )

    document.reference.update({
        "status": new_status,
        "updated_at": datetime.now(timezone.utc)
    })

    # NOTIFY REQUESTER

    buyer_email = item.get("buyer_email")

    chat_link = (
        APP_BASE_URL +
        "/?request_id=" +
        request_id
    )

    email_body = f"""
Hello {item.get('buyer_name', 'User')},

Your request on VASUDHA has been {new_status.lower()}.

Listing:
{item.get('listing_title', '')}

Owner:
{item.get('owner_name', '')}

You can open your private conversation here:

{chat_link}

Thank you,
VASUDHA Smart Agriculture Platform
"""

    send_email(
        buyer_email,
        "VASUDHA Request " + new_status,
        email_body
    )

    return jsonify({
        "success": True,
        "message": "Request " + new_status.lower() + ".",
        "status": new_status
    })


# -----------------------------------------
# PRIVATE MESSAGES
# ONLY OWNER AND REQUESTER
# -----------------------------------------

@app.route(
    "/api/requests/<request_id>/messages",
    methods=["GET", "POST"]
)
def request_messages(request_id):

    user, error = require_login()

    if error:
        return error

    database = get_database()

    document, item = get_request_document(request_id)

    if not item:
        return response_error("Request not found.", 404)

    # Only the two participants can access the chat.
    if user["id"] not in [
        item.get("buyer_id"),
        item.get("owner_id")
    ]:
        return response_error(
            "You are not allowed to access this conversation.",
            403
        )

    messages_ref = (
        database.collection("requests")
        .document(request_id)
        .collection("messages")
    )

    # GET MESSAGES

    if request.method == "GET":

        messages = []

        documents = messages_ref.stream()

        for message_document in documents:

            message = message_document.to_dict()
            message["id"] = message_document.id

            # Do not expose sender email in chat responses.
            message.pop("sender_email", None)

            messages.append(serialize(message))

        messages.sort(
            key=lambda message: message.get("created_at", "")
        )

        return jsonify({
            "messages": messages
        })

    # SEND MESSAGE

    data = request.get_json(silent=True) or {}

    message_text = data.get("message", "").strip()

    if not message_text:
        return response_error("Enter a message.")

    if len(message_text) > 5000:
        return response_error("Message is too long.")

    message_data = {
        "sender_id": user["id"],
        "sender_name": user["name"],
        "sender_email": user["email"],
        "message": message_text,
        "created_at": datetime.now(timezone.utc)
    }

    message_ref = messages_ref.document()

    message_ref.set(message_data)

    return jsonify({
        "success": True,
        "message": "Message sent successfully."
    }), 201


# -----------------------------------------
# DASHBOARD
# -----------------------------------------

@app.route("/api/dashboard", methods=["GET"])
def dashboard():

    user, error = require_login()

    if error:
        return error

    database = get_database()

    my_listings = []

    total_listings = 0
    land_listings = 0
    equipment_listings = 0

    listing_documents = database.collection(
        "listings"
    ).stream()

    for document in listing_documents:

        item = document.to_dict()

        if item.get("owner_id") != user["id"]:
            continue

        item["id"] = document.id

        total_listings += 1

        if item.get("category") == "land":
            land_listings += 1

        elif item.get("category") == "equipment":
            equipment_listings += 1

        # Do not expose owner's email in dashboard data.
        item.pop("owner_email", None)
        item.pop("owner_phone", None)

        my_listings.append(serialize(item))

    requests_sent = 0
    requests_received = 0

    request_documents = database.collection(
        "requests"
    ).stream()

    for document in request_documents:

        item = document.to_dict()

        if item.get("buyer_id") == user["id"]:
            requests_sent += 1

        if item.get("owner_id") == user["id"]:
            requests_received += 1

    return jsonify({
        "total_listings": total_listings,
        "land_listings": land_listings,
        "equipment_listings": equipment_listings,
        "requests_sent": requests_sent,
        "requests_received": requests_received,
        "my_listings": my_listings
    })


# -----------------------------------------
# ERROR HANDLERS
# -----------------------------------------

@app.errorhandler(404)
def page_not_found(error):
    return jsonify({
        "error": "Page or resource not found."
    }), 404


@app.errorhandler(413)
def file_too_large(error):
    return jsonify({
        "error": "File size is too large."
    }), 413


@app.errorhandler(500)
def server_error(error):
    return jsonify({
        "error": "An internal server error occurred."
    }), 500


# -----------------------------------------
# RUN APPLICATION
# -----------------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true"
    )