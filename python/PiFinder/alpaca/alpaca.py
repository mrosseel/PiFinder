from bottle import route, run, response, request
import json

# Placeholder for the actual telescope state and functionality
telescope_state = {
    "connected": False,
    # Add other necessary state properties here
}


def get_url(suffix):
    return f"/api/v1/telescope/<device_number:int>/{suffix}"


def check(a_request, ctID, status):
    print(f"ctID: {ctID=}")
    print(f"ctID: {a_request.query.ClientTransactionID=}")
    print(f"ctID: {a_request.query=}")
    return status


def std_res(a_request, status=200, value=None):
    ctID = a_request.query.ClientTransactionID
    ctIDi = int(ctID) if ctID else 0
    response.status = status
    response.status = check(a_request, ctIDi, status)
    result = {
        "ClientTransactionID": ctIDi,
        "ServerTransactionID": 0,
        "ErrorNumber": 0,
        "ErrorMessage": "",
    }
    if value:
        result["Value"] = value
    return result


# safe float conversion in case locale uses comma instead of points.
def _float(input: str):
    return float(input.replace(",", "."))


# Helper function to construct JSON responses
def alpaca_response(value=None, error_number=0, error_message=""):
    return {"Value": value, "ErrorNumber": error_number, "ErrorMessage": error_message}


@route("/management/apiversions", method="GET")
def api_versions():
    response.content_type = "application/json"
    return json.dumps([1])


@route(get_url("interfaceversion"), method="GET")
def get_interfaceversion(device_type, device_number, client_id, client_transaction_id):
    response.content_type = "application/json"
    return json.dumps(alpaca_response(telescope_state["connected"]))


@route(get_url("connected"), method="GET")
def get_connected(device_type, device_number, client_id, client_transaction_id):
    response.content_type = "application/json"
    return json.dumps(alpaca_response(telescope_state["connected"]))


@route(get_url("connected"), method="PUT")
def put_connected(device_number):
    global telescope_state
    telescope_state["connected"] = request.forms.get("Connected", default=False)
    ret = std_res(request)
    print(f"Connected: {ret=}")
    return ret


@route(get_url("slewing"), method="GET")
def get_slewing(device_number):
    response.content_type = "application/json"
    # Placeholder value, replace with actual slewing state
    return json.dumps(alpaca_response(False))


# Define additional endpoints using get_url
@route(get_url("alignmentmode"), method="GET")
def get_alignment_mode(device_number):
    response.content_type = "application/json"
    # Placeholder value, replace with actual alignment mode
    return json.dumps(alpaca_response(0))


@route(get_url("declination"), method="GET")
def get_declination(device_number):
    response.content_type = "application/json"
    # Placeholder value, replace with actual declination
    return json.dumps(alpaca_response(0.0))


@route(get_url("gps"), method="GET")
def get_gps(device_number):
    response.content_type = "application/json"
    # Placeholder value, replace with actual GPS state
    return json.dumps(alpaca_response(False))


@route(get_url("siderealtime"), method="GET")
def get_sidereal_time(device_number):
    response.content_type = "application/json"
    # Placeholder value, replace with actual sidereal time
    return json.dumps(alpaca_response(0.0))


# Add other mandatory and optional properties and methods here

run(host="0.0.0.0", port=11111)
