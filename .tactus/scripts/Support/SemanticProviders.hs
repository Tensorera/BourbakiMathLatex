{-# LANGUAGE OverloadedStrings #-}
module Support.SemanticProviders (fragmentProvider) where

import Clef
import Data.Aeson (toJSON)
import qualified Data.Aeson.KeyMap as KM

fragmentProvider :: ProviderRef
fragmentProvider = (providerRef "codex")
  { providerRefModel = Just "gpt-6.1-sol"
  , providerRefEffort = Just "xhigh"
  , providerRefOptions = KM.fromList
      [("executable", toJSON ("/home/tensorera/.local/bin/codex" :: String))]
  }
