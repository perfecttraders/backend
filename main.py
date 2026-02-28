"""FastAPI service for MT5 trading bridge."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from database import Account, Trade, User, get_db, init_db
from mt5_engine import MT5Engine, MT5EngineError

load_dotenv()

app = FastAPI(title="Perfect Traders MT5 Bridge", version="1.0.0")

SECRET_KEY = os.getenv("JWT_SECRET", "change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    mt5_login_id: str = Field(min_length=3, max_length=64)


class UserOut(BaseModel):
    id: int
    email: EmailStr


class OpenTradeRequest(BaseModel):
    account_id: int
    symbol: str = Field(min_length=2, max_length=20)
    volume: float = Field(gt=0)
    type: str = Field(pattern="^(buy|sell)$")


class AdjustBalanceRequest(BaseModel):
    account_id: int
    balance: Decimal


@app.on_event("startup")
def startup_event() -> None:
    init_db()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str | None = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError as exc:
        raise credentials_exception from exc

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user


@app.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> UserOut:
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(email=payload.email, hashed_password=get_password_hash(payload.password))
    db.add(user)
    db.flush()

    account = Account(user_id=user.id, balance=Decimal("0.00"), mt5_login_id=payload.mt5_login_id)
    db.add(account)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email)


@app.post("/auth/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> Token:
    user = db.query(User).filter(User.email == form_data.username).first()
    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = create_access_token(data={"sub": user.email})
    return Token(access_token=access_token)


@app.get("/price/{symbol}")
def get_price(symbol: str, _: User = Depends(get_current_user)) -> dict:
    try:
        tick = MT5Engine().get_price(symbol)
    except MT5EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"symbol": tick.symbol, "bid": tick.bid, "ask": tick.ask, "time": tick.time}


@app.post("/trade/open", status_code=status.HTTP_201_CREATED)
def open_trade(
    payload: OpenTradeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    account = db.query(Account).filter(Account.id == payload.account_id, Account.user_id == user.id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        mt5_result = MT5Engine().send_market_order(
            symbol=payload.symbol,
            volume=payload.volume,
            order_type=payload.type,
            comment=f"user={user.id};account={account.id}",
        )
    except MT5EngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    trade = Trade(
        account_id=account.id,
        ticket_id=str(mt5_result["ticket"]),
        symbol=payload.symbol.upper(),
        volume=payload.volume,
        type=payload.type,
        open_price=mt5_result["price"],
        status="open",
    )
    db.add(trade)
    db.commit()
    db.refresh(trade)

    return {
        "trade_id": trade.id,
        "ticket_id": trade.ticket_id,
        "symbol": trade.symbol,
        "volume": trade.volume,
        "type": trade.type,
        "open_price": trade.open_price,
        "status": trade.status,
    }


@app.post("/admin/adjust-balance")
def adjust_balance(
    payload: AdjustBalanceRequest,
    x_admin_secret: str = Header(default="", alias="X-ADMIN-SECRET"),
    db: Session = Depends(get_db),
) -> dict:
    if not ADMIN_SECRET:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET is not configured")

    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Invalid admin secret")

    account = db.query(Account).filter(Account.id == payload.account_id).first()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    account.balance = payload.balance
    db.commit()
    db.refresh(account)

    return {"account_id": account.id, "balance": str(account.balance), "message": "Balance updated"}
