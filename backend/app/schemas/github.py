from pydantic import BaseModel, ConfigDict, Field, SecretStr


class GitHubPATConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: SecretStr = Field(min_length=20, max_length=500, repr=False)
    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
