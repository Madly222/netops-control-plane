"""Utility outage monitors: water and electricity."""


from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from src.web.auth import get_current_user, UserInfo

router = APIRouter()


@router.get("/water/status")
async def get_water_status(user: UserInfo = Depends(get_current_user)):
    """Get water disconnection status."""
    try:
        from src.monitoring.water_monitor import get_status_dict, check_water_status
        import asyncio
        asyncio.create_task(check_water_status())
        return get_status_dict()
    except ImportError:
        return {"ok": True, "last_check": None, "matches_count": 0, "matches": [], "error": "Water module not installed"}
    except Exception as e:
        logger.error(f"Water status error: {e}")
        return {"ok": True, "last_check": None, "matches_count": 0, "matches": [], "error": str(e)}


@router.post("/water/check")
async def check_water_now(user: UserInfo = Depends(get_current_user)):
    """Force a water-outage check now."""
    try:
        from src.monitoring.water_monitor import check_water_status, get_status_dict
        await check_water_status(force=True)
        return get_status_dict()
    except Exception as e:
        logger.error(f"Water check error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/electric/status")
async def get_electric_status(user: UserInfo = Depends(get_current_user)):
    """Get the power-outage status."""
    try:
        from src.monitoring.electric_monitor import get_status_dict, check_electric_status
        import asyncio
        asyncio.create_task(check_electric_status())
        return get_status_dict()
    except ImportError:
        return {"ok": True, "last_check": None, "matches_count": 0, "matches": [], "error": "Electric module not installed"}
    except Exception as e:
        logger.error(f"Electric status error: {e}")
        return {"ok": True, "last_check": None, "matches_count": 0, "matches": [], "error": str(e)}


@router.post("/electric/check")
async def check_electric_now(user: UserInfo = Depends(get_current_user)):
    """Force a power-outage check now."""
    try:
        from src.monitoring.electric_monitor import check_electric_status, get_status_dict
        await check_electric_status(force=True)
        return get_status_dict()
    except Exception as e:
        logger.error(f"Electric check error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
