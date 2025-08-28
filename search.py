from mcp.server.fastmcp import FastMCP
from cache import redis_client
from typing import Dict, Any, List, Optional
from config import BACKEND_URL, SDS_HEADER_NAME
import logging
import requests
import uuid
import base64
import tempfile
import os

logger = logging.getLogger(__name__)

# Initialize MCP server with proper description
mcp = FastMCP(
    name="SDS Manager Search",
    instructions="SDS Manager Search API - Authentication required. Please login with your access token first using the login tool.",
    stateless_http=True,
)


@mcp.tool()
async def login(access_token: str) -> Dict[str, Any]:
    """
    Authenticate user with API key.

    Returns: Dictionary with authentication status, user information, and available actions:
        - status: "success" or "error"
        - message: Welcome message or error details
        - user_info: User details (id, email, name)
        - session_id: Session identifier for subsequent operations
        - next_action_suggestion: List of suggestion (Do not perform action, just show to user)
        - instruction: Error guidance (only for error cases)
    """
    session_id = str(uuid.uuid4())

    # Validate access token format (basic check)
    if not access_token or not isinstance(access_token, str) or len(access_token) < 10:
        return {
            "error": "Invalid access token format",
            "instruction": "Please provide a valid API key"
        }

    headers = {SDS_HEADER_NAME: f"{access_token}"}

    try:
        response = requests.get(
            f"{BACKEND_URL}/user/", headers=headers, timeout=10
        )

        if response.status_code == 200:
            user_info = response.json()
            redis_client.set(f"sds_mcp:{session_id}", {
                "access_token": access_token,
                "user_id": user_info.get("id"),
                "email": user_info.get("email"),
                "name": user_info.get("name", "User")
            })

            return {
                "status": "success",
                "message": "Login successful! Welcome to SDS Manager.",
                "user_info": {
                    "id": user_info.get("id"),
                    "email": user_info.get("email"),
                    "name": user_info.get("first_name", "") + " " + user_info.get("last_name", "")
                },
                "session_id": session_id,
                "next_action_suggestion": [
                    "Search action: search_global_database tool, search_customer_library tool",
                    "Location action: get_location tool, add_location tool, add_sds_to_location tool",
                    "Substance action: move_sds_to_location tool, copy_sds_substance tool, archive_sds_substance tool, archive_sds_substance tool",
                    "User action: check_auth_status tool"
                ],
            }
        elif response.status_code == 401:
            return {
                "status": "error",
                "error": "Authentication failed",
                "instruction": "Invalid or expired access token. Please provide a valid access token."
            }
        else:
            return {
                "status": "error",
                "error": f"Authentication failed with status {response.status_code}",
                "instruction": "Please check your access token and try again."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to authentication server. Please try again."
        }


@mcp.tool()
async def check_auth_status(session_id: str) -> Dict[str, Any]:
    """
    Check if the current session is authenticated.

    REQUIRED: User must be authenticated using the login tool first.
    """

    if not session_id:
        return {
            "status": "not_initialized",
            "authenticated": False,
            "instruction": "No session found. Please use the login tool to authenticate."
        }

    info = redis_client.get(f"sds_mcp:{session_id}")

    if info:
        return info
    else:
        return {
            "status": "not_authenticated",
            "authenticated": False,
            "instruction": "Please use the login tool with your access token to authenticate.",
            "example": "login(access_token='your_jwt_token_here')"
        }


@mcp.tool()
async def search_global_database(
    keyword: str, 
    page: int = 1, 
    page_size: int = 10, 
    language_code: Optional[str] = None,
    region_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Search for Safety Data Sheets (SDS) from the global database by keywords (Do not need authentication).
    
    IMPORTANT GUIDELINES:
    - If user ask to search without mentioning internally or globally, do search_global_database tool and search_customer_library tool (if authenticated).
    - Do not perform broader search. If no results, only show suggestion for user to choose.
    - Display in a table for the response results with columns: "ID", "Product Name", "Product Code", "Manufacturer Name", "Revision Date", "Language", "Regulation Area", "Public Link", "Discovery Link".
    - Auto convert to language/region code if user input language/region name (e.g., "English" -> "en", "Europe" -> "EU", etc.).
    """
    try:
        search_param = f"?search={keyword}&page={page}&page_size={page_size}"
        if language_code:
            search_param += f"&language_code={language_code}"
        if region_code:
            search_param += f"&region={region_code.upper()}"

        response = requests.get(
            f"{BACKEND_URL}/pdfs/{search_param}",
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            count = data.get("count", 0)
            return {
                "status": "success",
                "keyword": keyword,
                "results": results,
                "count": count,
                "next": data.get("next"),
                "previous": data.get("previous"),
                "facets": data.get("facets", {}),
                "page": page,
                "page_size": page_size
            }
        else:
            error_msg = response.json().get("error_message", None)
            if error_msg and len(error_msg) > 0:
                return {
                    "status": "error",
                    "error": error_msg[0],
                }
            
            return {
                "status": "error",
                "error": f"Failed to search SDS with status {response.status_code}",
                "instruction": "Failed to search SDS. Please try again."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error", 
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to global search service. Please try again."
        }


@mcp.tool()
async def search_customer_library(session_id: str, keyword: str, page: int = 1, page_size: int = 10) -> Dict[str, Any]:
    """
    Search for substances (SDS assigned to a location) from customer's library by keywords.

    REQUIRED: User must be authenticated using the login tool first.

    IMPORTANT GUIDELINES:
    - If user ask to search without mentioning internally or globally, do search_global_database tool and search_customer_library tool (if authenticated).

    Returns: List of substance information
    """
    if not session_id:
        return {
            "status": "error",
            "error": "No active session found",
            "instruction": "Please login first using the login tool with your access token"
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.get(
            f"{BACKEND_URL}/substance/?search={keyword}&page={page}&page_size={page_size}",
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            return {
                "status": "success",
                "keyword": keyword,
                "results": data.get("results", []),
                "count": data.get("count", 0)
            }
        elif response.status_code == 401:
            redis_client.delete(f"sds_mcp:{session_id}")
            return {
                "status": "error",
                "error": "Authentication expired",
                "instruction": "Your session has expired. Please login again with your access token."
            }
        else:
            error_msg = response.json().get("error_message", None)
            if error_msg and len(error_msg) > 0:
                return {
                    "status": "error",
                    "error": error_msg[0],
                }
            
            return {
                "status": "error",
                "error": f"Failed to search SDS with status {response.status_code}",
                "instruction": "Failed to search SDS. Please try again."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to search service. Please try again."
        }


@mcp.tool()
async def add_sds_to_location(session_id: str, sds_id: str, location_id: str) -> Dict[str, Any]:
    """
    Add an SDS document from the global database to a specific location.

    REQUIRED: User must be authenticated using the login tool first.

    IMPORTANT GUIDELINES:
    - If not found any information of location, ask user to provide location name.
    - If not found any information of SDS, ask user to provide SDS name.
    - When user input location name, call get_location tool to get all locations and filter with location name.
    - Always ask user to choose which location if multiple locations found.
    - When user input SDS name, call search_global_database tool with keyword as SDS name.
    - Always ask user to choose which SDS if multiple SDS found.

    RETURN: Information of the newly added substance
    """

    endpoint = f"{BACKEND_URL}/location/{location_id}/addSDS/"

    if not session_id:
        return {
            "status": "error",
            "error": "No active session found",
            "instruction": "Please login first using the login tool with your access token"
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={"sds_id": sds_id},
            timeout=10
        )

        if response.status_code in [200, 201]:
            return response.json()
        elif response.status_code == 401:
            redis_client.delete(f"sds_mcp:{session_id}")
            return {
                "status": "error",
                "error": "Authentication expired",
                "instruction": "Your session has expired. Please login again with your access token."
            }
        else:
            error_msg = response.json().get("error_message", None)
            if error_msg and len(error_msg) > 0:
                return {
                    "status": "error",
                    "error": error_msg[0],
                }
            
            return {
                "status": "error",
                "error": f"Failed to add SDS to location with status {response.status_code}",
                "instruction": "Failed to add SDS to location. Please verify the SDS and location."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to location service. Please try again."
        }


@mcp.tool()
async def move_sds_to_location(session_id: str, substance_id: str, location_id: str) -> Dict[str, Any]:
    """
    Move a substance (SDS assigned to a location) to a specific location.
    
    REQUIRED: User must be authenticated using the login tool first.

    IMPORTANT GUIDELINES:
    - If not found any information of location, ask user to provide location name.
    - If not found any information of SDS, ask user to provide SDS name.
    - When user input location name, call get_location tool to get all locations and filter with location name.
    - Always ask user to choose which location if multiple locations found.
    - When user input SDS name, call search_customer_library tool with keyword as SDS name.
    - Always ask user to choose which substance if multiple substance found.
    
    Return: Information of the moved substance
    """

    endpoint = f"{BACKEND_URL}/substance/{substance_id}/move/"

    if not session_id:
        return {
            "status": "error",
            "error": "No active session found",
            "instruction": "Please login first using the login tool with your access token"
        }
        
    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }
        
    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}
    
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={"department_id": location_id},
            timeout=10
        )
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 401:
            redis_client.delete(f"sds_mcp:{session_id}")
            return {
                "status": "error",
                "error": "Authentication expired",
                "instruction": "Your session has expired. Please login again with your access token."
            }
        else:
            error_msg = response.json().get("error_message", None)
            if error_msg and len(error_msg) > 0:
                return {
                    "status": "error",
                    "error": error_msg[0],
                }
            
            return {
                "status": "error",
                "error": f"Failed to add SDS to move SDS with status {response.status_code}",
                "instruction": "Failed to move SDS to location. Please verify the SDS and location."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to location service. Please try again."
        }


@mcp.tool()
async def copy_sds_substance(session_id: str, substance_id: str, location_id: str) -> Dict[str, Any]:
    """
    Copy a substance (SDS assigned to a location) and add to another location/department.

    Requires: User must be authenticated via the login tool first.

    IMPORTANT GUIDELINES:
    - If not found any information of location, ask user to provide location name.
    - If not found any information of SDS, ask user to provide SDS name.
    - When user input location name, call get_location tool to get all locations and filter with location name.
    - Always ask user to choose which location if multiple locations found.
    - When user input SDS name, call search_customer_library tool with keyword as SDS name.
    - Always ask user to choose which substance if multiple substance found.

    Return: Information of the added substance
    """

    endpoint = f"{BACKEND_URL}/substance/{substance_id}/copy/"

    if not session_id:
        return {
            "status": "error",
            "error": "No active session found",
            "instruction": "Please login first using the login tool with your access token"
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={"department_id": location_id},
            timeout=10
        )

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 401:
            redis_client.delete(f"sds_mcp:{session_id}")
            return {
                "status": "error",
                "error": "Authentication expired",
                "instruction": "Your session has expired. Please login again with your access token."
            }
        else:
            error_msg = response.json().get("error_message", None)
            if error_msg and len(error_msg) > 0:
                return {
                    "status": "error",
                    "error": error_msg[0],
                }
            
            return {
                "status": "error",
                "error": f"Failed to copy SDS to location with status {response.status_code}",
                "instruction": "Failed to copy SDS to location. Please verify the SDS and location."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to copy service. Please try again."
        }


@mcp.tool()
async def archive_sds_substance(session_id: str, substance_id: str) -> Dict[str, Any]:
    """
    Move a substance (SDS assigned to a location) to archive.
    Synonyms: delete substance, remove substance, etc.

    REQUIRED: User must be authenticated using the login tool first.

    IMPORTANT GUIDELINES:
    - If not found any information of SDS, ask user to provide SDS name.
    - When user input SDS name, call search_customer_library tool with keyword as SDS name.
    - Always ask user to choose which substance if multiple substance found.

    Return: Information of the archived substance
    """

    endpoint = f"{BACKEND_URL}/substance/{substance_id}/archive/"

    if not session_id:
        return {
            "status": "error",
            "error": "No active session found",
            "instruction": "Please login first using the login tool with your access token"
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.post(
            endpoint,
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            return response.json()
        elif response.status_code == 401:
            redis_client.delete(f"sds_mcp:{session_id}")
            return {
                "status": "error",
                "error": "Authentication expired",
                "instruction": "Your session has expired. Please login again with your access token."
            }
        else:
            error_msg = response.json().get("error_message", None)
            if error_msg and len(error_msg) > 0:
                return {
                    "status": "error",
                    "error": error_msg[0],
                }
            
            return {
                "status": "error",
                "error": f"Failed to archive substance with status {response.status_code}",
                "instruction": "Failed to archive substance. Please verify the Substance."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to archive service. Please try again."
        }


@mcp.tool()
async def get_location(session_id: str) -> List[Dict[str, Any]]:
    """
    Get location tree list for the current user.

    REQUIRED: User must be authenticated using the login tool first.

    IMPORTANT GUIDELINES:
    - Display in a tree structure (Similar to file explorer).
    """

    if not session_id:
        return {
            "status": "not_initialized",
            "authenticated": False,
            "instruction": "You haven't logged in yet. Please provide me your access token to authenticate."
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.get(
            f"{BACKEND_URL}/location/",
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            return response.json()
        else:
            return {
                "status": "error",
                "error": f"Get location structure error with status {response.status_code}",
                "instruction": "Failed to get location structure. Please try again or contact support."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to service. Please try again."
        }


@mcp.tool()
async def add_location(session_id: str, name: str, parent_department_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Add new location.

    REQUIRES: User must be authenticated using the login tool first.

    IMPORTANT GUIDELINES:
    - parent_department_id is None when creating root location.
    - When user not mentioning parent location, ask user to clarify whether it is root location or not.
    - If it is not root location, ask user to provide parent location.
    - When user input location name, call get_location tool to get all locations and filter with location name.
    - Always ask user to choose which location if multiple locations found.

    RETURN: A dictionary of the newly added location
    """
    if not session_id:
        return {
            "status": "not_initialized",
            "authenticated": False,
            "instruction": "You haven't logged in yet. Please provide me your access token to authenticate."
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.post(
            f"{BACKEND_URL}/location/",
            headers=headers,
            json={
                "name": name,
                "parent_department_id": parent_department_id
            },
            timeout=10
        )

        if response.status_code == 201:
            return response.json()
        else:
            return {
                "status": "error",
                "error": f"Add location error with status {response.status_code}",
                "instruction": "Failed to add location. Please try again or contact support."
            }
    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to service. Please try again."
        }


@mcp.tool(
    description="Get detailed information about an SDS substance in your inventory. Requires authentication via login tool first."
)
async def retrieve_substance_detail(substance_id: str, session_id: str) -> Dict[str, Any]:
    """
    Get comprehensive details about a specific SDS substance in your organization's inventory.

    This tool provides complete information about an SDS substance that's already been added to your 
    organization's locations. Use this to view detailed chemical information, hazard data, location details,
    and all other properties of a substance.

    REQUIRES: User must be authenticated using the login tool first.

    Arguments:
        substance_id: The unique identifier of the substance (obtained from customer library search results)
        session_id: Session ID from authentication

    Returns:
        Dictionary containing comprehensive substance details:
        
        Top-level fields:
        - id: Substance ID (int)
        - is_archived: Archive status (bool)
        - sds_id: SDS document ID (int)
        - public_view_url: Public viewing URL (str)
        - safety_summary_url: Safety summary URL (str)
        - language: Document language code (str, e.g. "no", "en")
        - product_name: Product/chemical name (str)
        - supplier_name: Supplier/manufacturer name (str)
        - product_code: Product code/catalog number (str)
        - revision_date: SDS revision date (str, YYYY-MM-DD format)
        - created_date: Record creation date (str, YYYY-MM-DD format)
        - hazard_sentences: H-codes comma-separated (str, e.g. "H302,H317,H319")
        - euh_sentences: EUH codes (str)
        - prevention_sentences: P-codes comma-separated (str)
        - substance_amount: Amount of substance (nullable)
        - substance_approval: Approval status (nullable)
        - nfpa: NFPA rating (nullable)
        - hmis: HMIS rating (nullable)
        - info_msg: Information message (nullable)
        - attachments: List of attached files (list)
        
        location: Dict with location information
        - id: Location ID (int)
        - name: Location name (str)
        
        icons: List of GHS pictogram icons
        - url: Icon image URL (str)
        - description: Icon description (str, e.g. "GHS07")
        - type: Icon type (str, typically "GHS")
        
        highest_risks: Dict with risk assessments
        - health_risk, safety_risk, environment_risk: Base risk levels (nullable)
        - health_risk_incl_ppe, safety_risk_incl_ppe, environment_risk_incl_ppe: Risk levels including PPE (nullable)
        
        highest_storage_risks: Dict with storage risk assessments
        - storage_safety_risk, storage_environment_risk: Storage risk levels (nullable)
        
        sds_info: Comprehensive SDS document information dict
        - uuid: SDS document UUID (str)
        - cas_no: CAS number (str)
        - ec_no: EC number (nullable str)
        - chemical_formula: Chemical formula (str)
        - version: SDS version (str)
        - health_risk, safety_risk, environment_risk: Numeric risk ratings (int)
        - signal_word: GHS signal word (str, e.g. "Fare", "Danger")
        - hazard_codes: List of detailed H-codes with descriptions
        - precautionary_codes: List of detailed P-codes with descriptions
        - sds_chemical: List of chemical components with CAS numbers and concentrations
        - regulations: List of regulatory listings (Prop 65, ECHA, SIN List, etc.)
        - ghs_pictogram_code: GHS codes comma-separated (str)
        - reach_reg_no: REACH registration number (str)
        - emergency_telephone_number: Emergency contact (str)
        - Many other technical and regulatory fields...
        
        sds_other_info: Detailed SDS sections breakdown by section numbers (2-16)
        - Keys are section numbers as strings ("2", "3", "4", etc.)
        - Values are lists of section content with tags, values, and literals
        - Contains parsed content from all 16 sections of the SDS document
        - Includes composition, first aid, fire fighting, handling, physical properties, toxicology, etc.

    Example Usage:
        # First search your customer library
        results = search_customer_library("acetone", session_id)
        substance_id = results["results"][0]["id"]  # Get substance ID from search
        
        # Then get detailed information
        details = retrieve_substance_detail(substance_id, session_id)

    Related Tools:
        - search_customer_library: Find substances to get their IDs
        - move_sds_to_location: Move substances between locations
        - copy_sds_substance: Copy substances to additional locations
    """

    if not session_id:
        return {
            "status": "error",
            "error": "No active session found",
            "instruction": "Please login first using the login tool with your access token"
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    endpoint = f"{BACKEND_URL}/substance/{substance_id}/"
    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.get(endpoint, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            return {
                "status": "success",
                "substance_details": data,
                "message": f"Successfully retrieved details for substance {substance_id}"
            }
        else:
            try:
                data = response.json()
            except ValueError:
                data = {}
            return {
                    "status": "error",
                    "error_code": data.get("error_code", "Unknown error"),
                    "error_message": data.get("error_message", "Unknown error"),
                    "instruction": (
                        f"Something went wrong. {data.get('error_message', 'Unknown error')}"
                    )
                }

    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to service. Please try again."
        }


@mcp.tool()
async def get_sdss_with_ingredients(session_id: str, query: str = "", page: int = 1, page_size: int = 10) -> Dict[str, Any]:
    """
    Get a paginated list of SDSs (Safety Data Sheets) that contain ingredients 
    on the restricted list.

    ARGUMENTS:
        session_id (str): Session ID obtained after authentication.
        query (str, optional): Search term to filter SDSs. 
            - If empty (""), returns all SDSs with restricted ingredients.
            - If provided, returns only matching SDSs with restricted ingredients.
        page (int, optional): Page number of results to fetch. Defaults to 1.
        page_size (int, optional): Number of SDSs per page. Defaults to 10.

    RETURN:
        dict: 
            - "results": List of SDSs with restricted ingredients
            - "page": Current page number
            - "page_size": Number of items per page
            - "has_more": Boolean flag indicating if more results are available

    NOTES FOR USERS:
        - Leave `query` empty to see all SDSs with restricted ingredients.
        - Provide a `query` to search only within restricted SDSs.
        - Use `page` and `page_size` to control pagination.
        - If `has_more` is True, you can load additional results by increasing `page`.
    """

    if not session_id:
        return {
            "status": "error",
            "error": "No active session found",
            "instruction": "Please login first using the login tool with your access token"
        }

    info = redis_client.get(f"sds_mcp:{session_id}")
    if not info:
        return {
            "status": "error",
            "error": "Access token not found in session",
            "instruction": "Session expired. Please login again using the login tool."
        }

    endpoint = f"{BACKEND_URL}/substance/?hazardous=true&search={query}&page={page}&page_size={page_size}"
    headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    try:
        response = requests.get(endpoint, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json()
        else:
            try:
                data = response.json()
            except ValueError:
                data = {}

            return {
                    "status": "error",
                    "error_code": data.get("error_code", "Unknown error"),
                    "error_message": data.get("error_message", "Unknown error"),
                    "instruction": (
                        f"Something went wrong. {data.get('error_message', 'Unknown error')}"
                    )
                }

    except requests.exceptions.RequestException as e:
        return {
            "status": "error",
            "error": f"Connection error: {str(e)}",
            "instruction": "Failed to connect to service. Please try again."
        }


@mcp.tool()
async def upload_sds_file_to_location(pdf_content_base64: str, department_id: str) -> dict:
    """
    Upload a SDS file to the specified location in the SDS Manager system.

    When user upload a SDS file, must convert it to base64 string and pass it to this tool.

    ARGUMENTS:
        pdf_content_base64 (str): Base64 encoded PDF content.
        department_id (str): ID of the department to upload the SDS file to.

    RETURN:
    """

    # info = redis_client.get(f"sds_mcp:{session_id}")

    # if not info:
    #     return {
    #         "status": "error",
    #         "error": "Access token not found in session",
    #         "instruction": "Session expired. Please login again using the login tool."
    #     }

    # headers = {SDS_HEADER_NAME: f"{info.get('access_token')}"}

    return {
        "pdf_content_base64": pdf_content_base64,
        "length": len(pdf_content_base64),
        "department_id": department_id,
    }

    # response = requests.post(
    #     f"{BACKEND_URL}/location/{department_id}/uploadSDS/",
    #     headers=headers,
    #     files={"imported_file": pdf_content}
    # )

    # if response.status_code == 200:
    #     return response.json()
    # else:
    #     return {
    #         "status": "error",
    #         "error": f"Add location error with status {response.status_code}",
    #         "instruction": "Failed to add location. Please try again or contact support."
    #     }


    