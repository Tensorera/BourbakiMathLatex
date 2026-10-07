{-# LANGUAGE OverloadedStrings #-}
module Main (main) where

import Clef
import Data.Aeson
import qualified Data.Aeson.KeyMap as KM
import qualified Data.ByteString.Lazy as BL
import Data.Text (Text)
import qualified Data.Text as T
import qualified Data.Text.Encoding as TE
import System.Directory (createDirectoryIfMissing, doesFileExist)
import System.Environment (getArgs)
import System.Exit (ExitCode(..))
import System.FilePath ((</>))
import System.IO (hSetEncoding, stdout, stderr, utf8)
import System.Process (readProcessWithExitCode)
import Support.SemanticProviders (fragmentProvider)

data Volume = Volume { order :: Int, job :: String, volume :: String }
instance FromJSON Volume where
  parseJSON = withObject "Volume" $ \o -> Volume <$> o .: "order" <*> o .: "job" <*> o .: "volume"
newtype Catalog = Catalog [Volume]
instance FromJSON Catalog where
  parseJSON = withObject "Catalog" $ \o -> Catalog <$> o .: "volumes"

helper :: [String] -> IO ()
helper arguments = do
  (status, out, err) <- readProcessWithExitCode "python" (".tactus/scripts/Support/semantic_fragments.py" : arguments) ""
  case status of
    ExitSuccess -> putStrLn ("[info] " <> T.unpack (T.takeEnd 600 (T.strip (T.pack out))))
    ExitFailure n -> fail ("helper exited " <> show n <> ": " <> take 1600 (out <> err))

planTask :: Task Text Value
planTask = jsonTask "bourbaki-semantic-fragment-copy-plan" $ \input -> T.unlines
  [ "You are the sole gpt-6.1-sol/xhigh reviewer for ONE complete bilingual Bourbaki volume."
  , "We are reorganizing files by existing chapter/section/subsection boundaries. Use COPY AND PASTE / exact byte slicing, never regenerate or rewrite the book."
  , "A deterministic helper owns copying the original English and Chinese source spans. Your ONLY output is the naming plan JSON. Do not edit any file or call tools, another agent/provider, OCR, or a translation service."
  , "The final pre/post split PDFs MUST remain strictly identical. Preserve every word, formula, command, heading, counter, image reference, list, proof, exercise, whitespace and source order. Do not change the provided boundaries or IDs."
  , "For each entry give a clear short English slug. Use only lowercase ASCII letters/digits and single hyphens; total slug length INCLUDING hyphens <=20 characters. Do not put structural numbers in the slug. Both languages use the same slug."
  , "Prefer conventional mathematical terms. Front matter, intro, exercises, appendix and back matter need concise role-appropriate names. Same-slug entries are allowed because IDs are distinct."
  , "Return ONLY {\"schema\":\"semantic-fragments-plan/v1\",\"order\":<input order>,\"entries\":[{\"id\":<exact supplied id>,\"slug\":<short English name>},...]}. Include every entry once in the original order."
  , input
  ]

compactInput :: Value -> Value
compactInput (Object o) = Object $ KM.fromList
  [ ("order", maybe Null id (KM.lookup "order" o))
  , ("volume", maybe Null id (KM.lookup "volume" o))
  , ("entries", case KM.lookup "entries" o of
      Just (Array es) -> Array (fmap compactEntry es)
      _ -> Null)
  ]
  where compactEntry (Object e) = Object $ KM.filterWithKey (\k _ -> k `elem` ["id","kind","context","title_en","title_zh","suggested_slug"]) e
        compactEntry e = e
compactInput v = v

runVolume :: Bool -> Volume -> Workflow Bool
runVolume planOnly v = do
  let folder = "work/semantic_fragments" </> job v
      planPath = folder </> "plan.json"
      arguments stage = [stage, "--order", show (order v)]
  liftIO $ putStrLn ("[state] preparing volume " <> show (order v) <> ": " <> volume v)
  liftIO $ helper (arguments "prepare")
  exists <- liftIO $ doesFileExist planPath
  if exists
    then liftIO $ putStrLn ("[info] retaining existing plan for " <> job v)
    else do
      raw <- liftIO $ BL.readFile (folder </> "naming-input.json")
      input <- case eitherDecode raw of
        Left e -> liftIO $ fail e
        Right value -> pure (TE.decodeUtf8 (BL.toStrict (encode (compactInput value))))
      result <- invokeWith fragmentProvider planTask input
      liftIO $ BL.writeFile planPath (encode result <> "\n")
  if planOnly then pure True else do
    liftIO $ helper (arguments "apply")
    liftIO $ helper (arguments "verify")
    liftIO $ putStrLn ("[state] fragment source/PDF hash gate passed: " <> job v)
    pure True

main :: IO ()
main = do
  hSetEncoding stdout utf8
  hSetEncoding stderr utf8
  args <- getArgs
  raw <- BL.readFile ".tactus/scripts/Support/volumes.json"
  Catalog allVolumes <- either fail pure (eitherDecode raw)
  let requested = [read n :: Int | (a,n) <- zip args (drop 1 args), a == "--order"]
      selected = if null requested then allVolumes else filter ((`elem` requested) . order) allVolumes
      planOnly = "--plan-only" `elem` args
  createDirectoryIfMissing True "work/semantic_fragments"
  results <- runTactus $ do
    liftIO $ putStrLn ("[state] semantic fragments: " <> show (length selected) <> " bilingual volumes; codex/gpt-6.1-sol/xhigh; concurrency=3")
    outcomes <- parallelAllBounded 3 [attempt (runVolume planOnly v) | v <- selected]
    liftIO $ mapM_ (\(v,outcome) -> case outcome of
      Right True -> pure ()
      other -> putStrLn ("[error] volume " <> show (order v) <> " " <> job v <> ": " <> show other)) (zip selected outcomes)
    _ <- requireBecause "one or more volume tasks need reconciliation; no automatic retry" (all succeeded) outcomes
    pure outcomes
  putStrLn ("[state] semantic-fragment phase completed: " <> show (length results))
  where succeeded (Right True) = True
        succeeded _ = False
