from src.config import Settings
from src.client import P3ASession
from src.quiz import answer_today

cfg  = Settings()                         # captcha_key 放 config.toml 或 Secret
sess = P3ASession(cfg)
sess.login()
print(answer_today(sess, captcha_api_key=cfg.captcha_key))
