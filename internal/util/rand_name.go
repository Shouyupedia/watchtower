package util

import (
	"crypto/rand"
	"math/big"
)

var letters = []rune("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")

// RandName Generates a random, 32-character, Docker-compatible container name.
func RandName() string {
	b := make([]rune, 32)
	max := big.NewInt(int64(len(letters)))
	for i := range b {
		n, err := rand.Int(rand.Reader, max)
		if err != nil {
			// crypto/rand failing means the system is unusable, there is no sensible fallback
			panic(err)
		}
		b[i] = letters[n.Int64()]
	}

	return string(b)
}
