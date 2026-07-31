from fastapi import HTTPException


class CompanyAlreadyExistsException(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=409,
            detail="Company with this website already exists."
        )